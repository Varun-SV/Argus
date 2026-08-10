"""Linux libvirt/QEMU provider for disposable Argus Capsules.

PR7's first non-Windows provider deliberately targets local ``qemu:///system``
KVM/libvirt. It preserves the same security shape as the Hyper-V provider:

* the golden image is never booted writable; each session gets a qcow2 overlay;
* a per-session libvirt network has no ``<forward>`` element, so it is isolated
  from the physical LAN;
* a per-session nwfilter permits only DHCP plus host→guest Argus control traffic;
* the secure guest agent still uses pinned HTTPS + per-session bearer rotation;
* failure retention is forensic disk/config preservation, not remote restart.

No Python libvirt binding is required; the provider uses argv-based ``virsh``
and ``qemu-img`` subprocesses so command data never passes through a shell.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from argus.capsule.base import (
    CapsuleError,
    CapsuleHandle,
    CapsuleProvider,
    CapsuleProviderCapabilities,
    CapsuleRequest,
    FailureCapsule,
)
from argus.capsule.files import validate_session_id


class _AmbiguousResourceOwnership(CapsuleError):
    """A libvirt mutation may have completed but ownership cannot be attested."""


class LibvirtProvider(CapsuleProvider):
    """Create isolated Linux Capsules using libvirt/QEMU/KVM."""

    provider_name = "libvirt"
    provider_capabilities = CapsuleProviderCapabilities(
        provider="libvirt",
        host_platforms=("linux",),
        guest_os=("linux",),
        secure_transport=True,
        network_isolation=True,
        explicit_transfers=True,
        failure_retention=True,
        # PR7 intentionally fails closed instead of pretending isolated-network
        # mode can express Hyper-V's destination-CIDR egress allowlist semantics.
        egress_allowlist=False,
    )
    _DEFAULT_SYSTEM_VM_ROOT = Path("/var/lib/libvirt/images/argus-capsules")
    _DEFAULT_NETWORK_POOL = ipaddress.ip_network("10.240.0.0/12")
    _RFC1918_POOLS = (
        ipaddress.ip_network("10.0.0.0/8"),
        ipaddress.ip_network("172.16.0.0/12"),
        ipaddress.ip_network("192.168.0.0/16"),
    )
    _SESSION_PREFIXLEN = 24
    _PROCESS_NETWORK_ALLOCATION_LOCK = threading.Lock()

    def __init__(
        self,
        *,
        runner: Optional[Callable[[Sequence[str], float], str]] = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._sleeper = sleeper
        self._clock = clock
        self._virsh: Optional[str] = None
        self._qemu_img: Optional[str] = None
        self._ip: Optional[str] = None

    # ---- command boundary ---------------------------------------------

    def _ensure_host(self, uri: str) -> None:
        if uri != "qemu:///system":
            raise CapsuleError(
                "PR7 libvirt Capsules require local qemu:///system; remote/session "
                "libvirt URIs do not provide the audited network-isolation boundary"
            )
        if self._runner is not None:
            return
        if not sys.platform.startswith("linux"):
            raise CapsuleError("libvirt/QEMU Capsules require a Linux host")
        self._virsh = shutil.which("virsh")
        self._qemu_img = shutil.which("qemu-img")
        self._ip = shutil.which("ip")
        if not self._virsh:
            raise CapsuleError("virsh is required for the libvirt Capsule provider")
        if not self._qemu_img:
            raise CapsuleError("qemu-img is required for the libvirt Capsule provider")
        if not self._ip:
            raise CapsuleError(
                "iproute2 'ip' is required to attest host routes before libvirt subnet allocation"
            )
        self._run([self._virsh, "-c", uri, "uri"], 15)
        self._run([self._virsh, "-c", uri, "version"], 15)

    def _run(self, args: Sequence[str], timeout_seconds: float = 30.0) -> str:
        argv = [str(item) for item in args]
        if self._runner is not None:
            return str(self._runner(tuple(argv), timeout_seconds)).strip()
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CapsuleError(f"libvirt/QEMU invocation failed: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown libvirt error").strip()
            raise CapsuleError(f"libvirt/QEMU command failed: {detail[:800]}")
        return completed.stdout.strip()

    def _virsh_cmd(self, uri: str, *args: str, timeout: float = 30.0) -> str:
        binary = self._virsh or "virsh"
        return self._run([binary, "-c", uri, *args], timeout)

    def _qemu_img_cmd(self, *args: str, timeout: float = 30.0) -> str:
        binary = self._qemu_img or "qemu-img"
        return self._run([binary, *args], timeout)

    # ---- deterministic provider resources -----------------------------

    @staticmethod
    def _validated_session_id(value: str) -> str:
        raw = str(value or "")
        session_id = validate_session_id(raw)
        if session_id != raw:
            raise CapsuleError(
                "Capsule session id must already be canonical and contain no surrounding whitespace"
            )
        return session_id

    @staticmethod
    def _resource_suffix(session_id: str) -> str:
        # Resource names are derived from the complete session id, not a shared
        # prefix. Twenty hex characters gives an 80-bit collision boundary while
        # keeping generated libvirt names readable and well below practical limits.
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:20]

    @classmethod
    def _resource_names(cls, session_id: str) -> tuple[str, str, str, str]:
        suffix = cls._resource_suffix(session_id)
        return (
            f"Argus-{suffix}",
            f"argus-{suffix}",
            f"argus-{suffix}-control",
            f"ar{suffix[:10]}",  # Linux interface names must fit IFNAMSIZ.
        )

    @staticmethod
    def _mac_for(session_id: str) -> str:
        digest = hashlib.sha256(("mac:" + session_id).encode("utf-8")).digest()
        return f"52:54:00:{digest[0]:02x}:{digest[1]:02x}:{digest[2]:02x}"

    @classmethod
    def _network_pool(cls, configured: str) -> ipaddress.IPv4Network:
        """Validate and return the RFC1918 pool used for /24 session networks."""
        raw = str(configured or "").strip()
        if not raw:
            return cls._DEFAULT_NETWORK_POOL
        try:
            network = ipaddress.ip_network(raw, strict=True)
        except ValueError as exc:
            raise CapsuleError(
                f"capsule.libvirt_network_cidr must be a canonical IPv4 network: {configured!r}"
            ) from exc
        if not isinstance(network, ipaddress.IPv4Network):
            raise CapsuleError("capsule.libvirt_network_cidr must be IPv4")
        if not any(network.subnet_of(private) for private in cls._RFC1918_POOLS):
            raise CapsuleError(
                "capsule.libvirt_network_cidr must be contained in RFC1918 private space "
                "(10/8, 172.16/12, or 192.168/16)"
            )
        if network.prefixlen > cls._SESSION_PREFIXLEN:
            raise CapsuleError(
                "capsule.libvirt_network_cidr must contain at least one /24 session subnet"
            )
        return network

    @classmethod
    def _candidate_networks(
        cls,
        session_id: str,
        pool: ipaddress.IPv4Network,
    ):
        slots = 1 << (cls._SESSION_PREFIXLEN - pool.prefixlen)
        digest = hashlib.sha256(("network:" + session_id).encode("utf-8")).digest()
        start = int.from_bytes(digest[:8], "big") % slots
        base = int(pool.network_address)
        for offset in range(slots):
            slot = (start + offset) % slots
            yield ipaddress.ip_network(
                (base + (slot << (32 - cls._SESSION_PREFIXLEN)), cls._SESSION_PREFIXLEN)
            )

    @classmethod
    def _default_network(cls, session_id: str) -> ipaddress.IPv4Network:
        return next(cls._candidate_networks(session_id, cls._DEFAULT_NETWORK_POOL))

    @classmethod
    def _network_for(cls, session_id: str, configured: str) -> ipaddress.IPv4Network:
        """Compatibility helper returning the deterministic first /24 in a pool.

        Real allocation uses :meth:`_allocate_network`, which also rejects
        overlap with every currently defined libvirt network and explicit host
        route.
        """
        return next(cls._candidate_networks(session_id, cls._network_pool(configured)))

    @staticmethod
    def _network_from_xml(raw: str, name: str) -> list[ipaddress.IPv4Network]:
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as exc:
            raise CapsuleError(f"libvirt returned invalid XML for network {name!r}") from exc
        result: list[ipaddress.IPv4Network] = []
        for ip_node in root.findall("ip"):
            address = str(ip_node.attrib.get("address") or "").strip()
            if not address:
                continue
            prefix = str(ip_node.attrib.get("prefix") or "").strip()
            netmask = str(ip_node.attrib.get("netmask") or "").strip()
            suffix = prefix or netmask
            if not suffix:
                continue
            try:
                interface = ipaddress.ip_interface(f"{address}/{suffix}")
            except ValueError as exc:
                raise CapsuleError(
                    f"libvirt network {name!r} reported invalid IPv4 configuration"
                ) from exc
            if isinstance(interface, ipaddress.IPv4Interface):
                result.append(interface.network)
        return result

    def _defined_networks(self, uri: str) -> list[ipaddress.IPv4Network]:
        names = self._list_names(
            self._virsh_cmd(uri, "net-list", "--all", "--name", timeout=15)
        )
        occupied: list[ipaddress.IPv4Network] = []
        for name in sorted(names):
            raw = self._virsh_cmd(uri, "net-dumpxml", name, timeout=15)
            occupied.extend(self._network_from_xml(raw, name))
        return occupied

    @staticmethod
    def _route_networks_from_output(raw: str) -> list[ipaddress.IPv4Network]:
        """Parse explicit IPv4 routes from ``ip -4 route show table all``.

        The default route is intentionally ignored: a Capsule connected /24 is
        expected to be more specific than 0/0. Every other explicit route,
        including VPN and policy-table prefixes, is treated as occupied because
        installing a connected Capsule route would otherwise override it.
        """
        route_types = {
            "unicast",
            "local",
            "broadcast",
            "multicast",
            "throw",
            "unreachable",
            "prohibit",
            "blackhole",
            "nat",
            "anycast",
        }
        result: list[ipaddress.IPv4Network] = []
        for line in str(raw or "").splitlines():
            fields = line.split()
            if not fields:
                continue
            index = 1 if fields[0].lower() in route_types else 0
            if index >= len(fields):
                continue
            destination = fields[index]
            if destination.lower() == "default":
                continue
            try:
                network = ipaddress.ip_network(destination, strict=False)
            except ValueError:
                continue
            if isinstance(network, ipaddress.IPv4Network):
                result.append(network)
        return result

    def _host_route_networks(self) -> list[ipaddress.IPv4Network]:
        raw = self._run(
            [self._ip or "ip", "-4", "route", "show", "table", "all"],
            15,
        )
        return self._route_networks_from_output(raw)

    def _occupied_networks(self, uri: str) -> list[ipaddress.IPv4Network]:
        return [*self._defined_networks(uri), *self._host_route_networks()]

    def _allocate_network(
        self,
        uri: str,
        session_id: str,
        configured: str,
    ) -> ipaddress.IPv4Network:
        pool = self._network_pool(configured)
        occupied = self._occupied_networks(uri)
        for candidate in self._candidate_networks(session_id, pool):
            if not any(candidate.overlaps(existing) for existing in occupied):
                return candidate
        raise CapsuleError(
            f"no free /24 Capsule subnet remains in libvirt_network_cidr pool {pool}"
        )

    @contextmanager
    def _network_allocation_lock(self, uri: str):
        """Serialize Argus subnet selection through successful ``net-create``.

        The process-local lock prevents thread races. On Linux, an advisory
        ``flock`` on a well-known URI-derived file extends that boundary across
        Argus processes. The lock is intentionally held until libvirt has
        created the selected network, at which point the network itself is the
        durable reservation visible to later allocators. A crashed process
        releases the file lock automatically and leaves no stale reservation.
        """
        with self._PROCESS_NETWORK_ALLOCATION_LOCK:
            if not sys.platform.startswith("linux"):
                yield
                return

            try:
                import fcntl
            except ImportError as exc:  # pragma: no cover - Linux always ships fcntl.
                raise CapsuleError("Linux libvirt subnet allocation requires fcntl flock") from exc

            lock_hash = hashlib.sha256(uri.encode("utf-8")).hexdigest()[:20]
            lock_path = Path("/tmp") / f"argus-libvirt-{lock_hash}.network.lock"
            flags = os.O_RDWR | os.O_CREAT
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                fd = os.open(lock_path, flags, 0o600)
            except OSError as exc:
                raise CapsuleError(
                    f"cannot open libvirt subnet allocation lock {lock_path}: {exc}"
                ) from exc
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode):
                    raise CapsuleError(
                        f"libvirt subnet allocation lock is not a regular file: {lock_path}"
                    )
                if hasattr(os, "geteuid") and info.st_uid != os.geteuid():
                    raise CapsuleError(
                        f"libvirt subnet allocation lock is owned by another user: {lock_path}"
                    )
                if stat.S_IMODE(info.st_mode) & 0o022:
                    raise CapsuleError(
                        f"libvirt subnet allocation lock is writable by group/other: {lock_path}"
                    )
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def _normalize_arch(value: str) -> str:
        normalized = str(value or "").strip().lower()
        aliases = {
            "amd64": "x86_64",
            "x64": "x86_64",
            "arm64": "aarch64",
        }
        normalized = aliases.get(normalized, normalized)
        if normalized not in {"x86_64", "aarch64"}:
            raise CapsuleError(
                f"PR7 libvirt Capsules support same-architecture x86_64/aarch64 guests, got {normalized!r}"
            )
        return normalized

    @classmethod
    def _host_arch(cls, configured: str) -> str:
        actual = cls._normalize_arch(platform.machine())
        raw = str(configured or "").strip()
        requested = actual if not raw else cls._normalize_arch(raw)
        if requested != actual:
            raise CapsuleError(
                "PR7 libvirt Capsules use KVM and require guest architecture to match the host; "
                f"host={actual!r}, requested={requested!r}"
            )
        return actual

    @classmethod
    def _resolved_vm_root(cls, configured: str) -> Path:
        """Return storage suitable for a system libvirt/QEMU process.

        The generic Capsule default lives under the invoking user's home, which
        a ``qemu:///system`` worker commonly cannot traverse. Libvirt therefore
        defaults to the system image hierarchy. Sites can provide ``vm_root``
        explicitly when they have provisioned equivalent ownership/ACL access.
        """
        raw = str(configured or "").strip()
        if raw:
            return Path(raw).expanduser().resolve()
        return cls._DEFAULT_SYSTEM_VM_ROOT

    @staticmethod
    def _write_xml(path: Path, root: ET.Element) -> None:
        tree = ET.ElementTree(root)
        try:
            ET.indent(tree, space="  ")
        except AttributeError:  # pragma: no cover - Python >=3.10 in Argus.
            pass
        tree.write(path, encoding="utf-8", xml_declaration=True)

    @staticmethod
    def _network_xml(
        network_name: str,
        bridge_name: str,
        network: ipaddress.IPv4Network,
        mac: str,
        guest_ip: str,
        vm_name: str,
    ) -> ET.Element:
        root = ET.Element("network")
        ET.SubElement(root, "name").text = network_name
        ET.SubElement(root, "bridge", {"name": bridge_name, "stp": "on", "delay": "0"})
        ET.SubElement(root, "dns", {"enable": "no"})
        gateway = str(network.network_address + 1)
        ip = ET.SubElement(
            root,
            "ip",
            {"address": gateway, "netmask": str(network.netmask)},
        )
        dhcp = ET.SubElement(ip, "dhcp")
        ET.SubElement(
            dhcp,
            "host",
            {"mac": mac, "name": vm_name.lower(), "ip": guest_ip},
        )
        # Deliberately no <forward>: libvirt defines this as an isolated network.
        return root

    @staticmethod
    def _filter_xml(
        filter_name: str,
        host_ip: str,
        guest_ip: str,
        guest_port: int,
    ) -> ET.Element:
        root = ET.Element("filter", {"name": filter_name, "chain": "root"})

        arp = ET.SubElement(root, "rule", {"action": "accept", "direction": "inout", "priority": "100"})
        ET.SubElement(arp, "arp")

        dhcp_out = ET.SubElement(root, "rule", {"action": "accept", "direction": "out", "priority": "200"})
        ET.SubElement(dhcp_out, "udp", {"srcportstart": "68", "dstportstart": "67"})
        dhcp_in = ET.SubElement(root, "rule", {"action": "accept", "direction": "in", "priority": "201"})
        ET.SubElement(dhcp_in, "udp", {"srcportstart": "67", "dstportstart": "68"})

        control_in = ET.SubElement(root, "rule", {"action": "accept", "direction": "in", "priority": "300"})
        ET.SubElement(
            control_in,
            "tcp",
            {
                "srcipaddr": host_ip,
                "dstipaddr": guest_ip,
                "dstportstart": str(int(guest_port)),
                "state": "NEW,ESTABLISHED",
            },
        )
        control_out = ET.SubElement(root, "rule", {"action": "accept", "direction": "out", "priority": "301"})
        ET.SubElement(
            control_out,
            "tcp",
            {
                "srcipaddr": guest_ip,
                "dstipaddr": host_ip,
                "srcportstart": str(int(guest_port)),
                "state": "ESTABLISHED",
            },
        )

        ET.SubElement(root, "rule", {"action": "drop", "direction": "inout", "priority": "1000"})
        return root

    @staticmethod
    def _domain_xml(
        vm_name: str,
        overlay: Path,
        network_name: str,
        filter_name: str,
        mac: str,
        memory_mb: int,
        cpu_count: int,
        arch: str,
        machine: str,
    ) -> ET.Element:
        root = ET.Element("domain", {"type": "kvm"})
        ET.SubElement(root, "name").text = vm_name
        ET.SubElement(root, "memory", {"unit": "MiB"}).text = str(int(memory_mb))
        ET.SubElement(root, "currentMemory", {"unit": "MiB"}).text = str(int(memory_mb))
        ET.SubElement(root, "vcpu", {"placement": "static"}).text = str(int(cpu_count))

        os_attrs = {"firmware": "efi"} if arch == "aarch64" else {}
        os_node = ET.SubElement(root, "os", os_attrs)
        type_attrs = {"arch": arch}
        if machine:
            type_attrs["machine"] = machine
        elif arch == "aarch64":
            type_attrs["machine"] = "virt"
        ET.SubElement(os_node, "type", type_attrs).text = "hvm"
        ET.SubElement(os_node, "boot", {"dev": "hd"})

        ET.SubElement(root, "clock", {"offset": "utc"})
        ET.SubElement(root, "on_poweroff").text = "destroy"
        ET.SubElement(root, "on_reboot").text = "restart"
        ET.SubElement(root, "on_crash").text = "destroy"

        devices = ET.SubElement(root, "devices")
        disk = ET.SubElement(devices, "disk", {"type": "file", "device": "disk"})
        ET.SubElement(disk, "driver", {"name": "qemu", "type": "qcow2"})
        ET.SubElement(disk, "source", {"file": str(overlay)})
        ET.SubElement(disk, "target", {"dev": "vda", "bus": "virtio"})

        interface = ET.SubElement(devices, "interface", {"type": "network"})
        ET.SubElement(interface, "mac", {"address": mac})
        ET.SubElement(interface, "source", {"network": network_name})
        ET.SubElement(interface, "model", {"type": "virtio"})
        ET.SubElement(interface, "filterref", {"filter": filter_name})

        serial = ET.SubElement(devices, "serial", {"type": "pty"})
        ET.SubElement(serial, "target", {"port": "0"})
        console = ET.SubElement(devices, "console", {"type": "pty"})
        ET.SubElement(console, "target", {"type": "serial", "port": "0"})
        # Desktop Linux golden images should run the test session under Xvfb;
        # no SPICE/VNC channel is created, avoiding an extra host↔guest control path.
        return root

    # ---- validation ----------------------------------------------------

    def _validate_settings(self, request: CapsuleRequest) -> tuple[str, str]:
        settings = request.settings
        provider = (settings.provider or "auto").lower().strip()
        if provider not in {"auto", "libvirt", "qemu", "kvm"}:
            raise CapsuleError(f"LibvirtProvider cannot handle provider {settings.provider!r}")
        guest_os = (settings.guest_os or "auto").lower().strip()
        if guest_os not in {"auto", "linux"}:
            raise CapsuleError("libvirt/QEMU Capsules currently support guest_os=linux only")
        if not settings.guest_token:
            raise CapsuleError(
                "Capsule bootstrap token is missing; set ARGUS_CAPSULE_GUEST_TOKEN on the host"
            )
        if settings.allow_external_switch:
            raise CapsuleError("libvirt Capsules do not permit external/bridged host networking")
        if (settings.network_mode or "host_only").lower().strip() != "host_only":
            raise CapsuleError(
                "PR7 libvirt provider supports network_mode=host_only only; "
                "destination egress allowlists are not yet implemented"
            )
        if settings.egress_allowlist:
            raise CapsuleError("PR7 libvirt provider does not support egress_allowlist yet")
        if not settings.allow_dhcp:
            raise CapsuleError(
                "PR7 libvirt provider requires allow_dhcp=true for its fixed per-session lease"
            )
        if settings.memory_mb < 1024:
            raise CapsuleError("capsule.memory_mb must be at least 1024")
        if settings.cpu_count < 1:
            raise CapsuleError("capsule.cpu_count must be at least 1")
        if not (1 <= settings.guest_port <= 65535):
            raise CapsuleError("capsule.guest_port must be between 1 and 65535")
        if settings.guest_input_mode not in {"safe", "semantic", "physical", "legacy"}:
            raise CapsuleError("guest_input_mode must be safe/semantic or physical/legacy")

        transport = (settings.guest_transport or "https").lower().strip()
        if transport not in {"https", "http"}:
            raise CapsuleError("capsule.guest_transport must be https or http")
        if transport == "https":
            ca = settings.resolved_guest_ca_cert
            if ca is None or not ca.is_file():
                raise CapsuleError(
                    "HTTPS Capsule control requires guest_ca_cert pointing to the "
                    "dedicated guest CA/self-signed certificate"
                )
        elif not settings.allow_insecure_http:
            raise CapsuleError(
                "plain HTTP Capsule control is disabled; use HTTPS or explicitly "
                "allow insecure HTTP only for disposable development"
            )

        uri = str(settings.libvirt_uri or "qemu:///system").strip()
        self._ensure_host(uri)
        return transport, uri

    # ---- lifecycle -----------------------------------------------------

    def _base_image_format(self, image: Path) -> str:
        raw = self._qemu_img_cmd("info", "--output=json", str(image), timeout=20)
        try:
            data = json.loads(raw)
            fmt = str(data.get("format") or "").strip().lower()
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CapsuleError("qemu-img did not return valid image metadata") from exc
        if fmt not in {"qcow2", "raw"}:
            raise CapsuleError(
                f"PR7 libvirt golden images must be qcow2 or raw, got {fmt or 'unknown'!r}"
            )
        return fmt

    @staticmethod
    def _validate_requested_address(value: str, expected: str) -> None:
        requested = str(value or "").strip()
        if not requested:
            return
        try:
            normalized_address = ipaddress.ip_address(requested)
        except ValueError as exc:
            raise CapsuleError(f"capsule.guest_address must be a valid IPv4 address: {requested!r}") from exc
        if not isinstance(normalized_address, ipaddress.IPv4Address):
            raise CapsuleError("capsule.guest_address must be IPv4 for the PR7 libvirt provider")
        normalized = str(normalized_address)
        if normalized != expected:
            raise CapsuleError(
                "libvirt guest_address must match the fixed per-session DHCP lease "
                f"{expected!r}, got {normalized!r}"
            )

    def _reported_guest_ipv4s(self, uri: str, vm_name: str) -> list[str]:
        raw = self._virsh_cmd(
            uri,
            "domifaddr",
            vm_name,
            "--source",
            "lease",
            "--full",
            timeout=15,
        )
        addresses: list[str] = []
        for token in raw.replace("\n", " ").split():
            if "/" not in token:
                continue
            try:
                interface = ipaddress.ip_interface(token)
            except ValueError:
                continue
            address = interface.ip
            if (
                isinstance(address, ipaddress.IPv4Address)
                and not address.is_loopback
                and not address.is_link_local
                and not address.is_unspecified
            ):
                value = str(address)
                if value not in addresses:
                    addresses.append(value)
        return addresses

    def _wait_for_guest_address(
        self,
        uri: str,
        vm_name: str,
        expected: str,
        timeout_seconds: float,
    ) -> str:
        deadline = self._clock() + max(1.0, float(timeout_seconds))
        while self._clock() < deadline:
            addresses = self._reported_guest_ipv4s(uri, vm_name)
            if expected in addresses:
                return expected
            self._sleeper(1.0)
        raise CapsuleError(
            f"Capsule {vm_name!r} did not acquire its attested libvirt DHCP lease "
            f"{expected!r} within {timeout_seconds:.0f}s"
        )

    @staticmethod
    def _list_names(raw: str) -> set[str]:
        return {line.strip() for line in raw.splitlines() if line.strip()}

    @staticmethod
    def _nwfilter_names(raw: str) -> set[str]:
        """Parse regular ``virsh nwfilter-list`` tabular output.

        Unlike domain/network list commands, ``nwfilter-list`` has no ``--name``
        option. Argus-generated filter names contain no whitespace, so the final
        table column is a deterministic selector.
        """
        names: set[str] = set()
        for line in raw.splitlines():
            value = line.strip()
            if not value:
                continue
            lowered = value.lower()
            if lowered.startswith("uuid") or set(value) <= {"-", " "}:
                continue
            fields = value.split()
            if len(fields) >= 2:
                names.add(fields[-1])
        return names

    def _resource_present(self, uri: str, kind: str, name: str) -> bool:
        if kind == "domain":
            return name in self._list_names(
                self._virsh_cmd(uri, "list", "--all", "--name", timeout=15)
            )
        if kind == "network":
            return name in self._list_names(
                self._virsh_cmd(uri, "net-list", "--all", "--name", timeout=15)
            )
        if kind == "filter":
            return name in self._nwfilter_names(
                self._virsh_cmd(uri, "nwfilter-list", timeout=15)
            )
        raise CapsuleError(f"unknown libvirt resource kind: {kind}")

    def _create_owned_resource(
        self,
        uri: str,
        kind: str,
        name: str,
        owned_resources: set[str],
        command: str,
        xml_path: Path,
        *,
        timeout: float,
    ) -> None:
        """Create one resource and reconcile response-loss ambiguity.

        Names were proven absent before mutation. If ``virsh`` fails or times
        out after libvirt committed the operation, probe the exact generated
        name. A present object is claimed for rollback. If the probe itself
        fails, ownership is unknowable and storage must be preserved rather
        than deleting files that a possibly-live resource still references.
        """
        try:
            self._virsh_cmd(uri, command, str(xml_path), timeout=timeout)
        except Exception as create_exc:
            try:
                if self._resource_present(uri, kind, name):
                    owned_resources.add(kind)
            except Exception as probe_exc:
                raise _AmbiguousResourceOwnership(
                    f"could not reconcile {kind} {name!r} after ambiguous {command} failure: "
                    f"create={create_exc}; probe={probe_exc}"
                ) from create_exc
            raise
        owned_resources.add(kind)

    def _assert_resources_available(
        self,
        uri: str,
        vm_name: str,
        network_name: str,
        filter_name: str,
    ) -> None:
        """Reject provider-name collisions before Argus allocates mutable state."""
        collisions: list[str] = []
        domains = self._list_names(
            self._virsh_cmd(uri, "list", "--all", "--name", timeout=15)
        )
        if vm_name in domains:
            collisions.append(f"domain {vm_name!r}")
        networks = self._list_names(
            self._virsh_cmd(uri, "net-list", "--all", "--name", timeout=15)
        )
        if network_name in networks:
            collisions.append(f"network {network_name!r}")
        filters = self._nwfilter_names(
            self._virsh_cmd(uri, "nwfilter-list", timeout=15)
        )
        if filter_name in filters:
            collisions.append(f"nwfilter {filter_name!r}")
        if collisions:
            raise CapsuleError(
                "libvirt Capsule resource collision; refusing to modify pre-existing "
                + ", ".join(collisions)
            )

    def _cleanup_resources(
        self,
        uri: str,
        vm_name: str,
        network_name: str,
        filter_name: str,
        root: Path,
        *,
        remove_storage: bool,
        owned_resources: Optional[set[str]] = None,
        remove_nvram: bool = False,
    ) -> Optional[Exception]:
        """Remove only resources owned by this allocation attempt when supplied.

        ``owned_resources=None`` is reserved for cleanup of a successfully
        returned Capsule handle, whose provider ownership is already established.
        Creation rollback passes an explicit set that is populated only after
        each corresponding libvirt create/define operation succeeds or is
        reconciled as committed after an ambiguous command failure.
        """
        owns = lambda kind: owned_resources is None or kind in owned_resources
        try:
            if owns("domain"):
                domains = self._list_names(
                    self._virsh_cmd(uri, "list", "--all", "--name", timeout=15)
                )
                if vm_name in domains:
                    active = self._list_names(
                        self._virsh_cmd(uri, "list", "--name", timeout=15)
                    )
                    if vm_name in active:
                        self._virsh_cmd(uri, "destroy", vm_name, timeout=45)
                    undefine_args = ["undefine", vm_name]
                    if remove_nvram:
                        undefine_args.append("--nvram")
                    self._virsh_cmd(uri, *undefine_args, timeout=30)

            if owns("network"):
                networks = self._list_names(
                    self._virsh_cmd(uri, "net-list", "--all", "--name", timeout=15)
                )
                if network_name in networks:
                    active_networks = self._list_names(
                        self._virsh_cmd(uri, "net-list", "--name", timeout=15)
                    )
                    if network_name in active_networks:
                        self._virsh_cmd(uri, "net-destroy", network_name, timeout=30)

            if owns("filter"):
                filters = self._nwfilter_names(
                    self._virsh_cmd(uri, "nwfilter-list", timeout=15)
                )
                if filter_name in filters:
                    self._virsh_cmd(uri, "nwfilter-undefine", filter_name, timeout=30)
        except Exception as exc:
            return CapsuleError(
                f"libvirt resource cleanup failed: {exc}; session storage preserved at {root}"
            )

        if remove_storage:
            try:
                shutil.rmtree(root)
            except FileNotFoundError:
                pass
            except OSError as exc:
                return CapsuleError(f"session storage removal failed: {exc}")
        return None

    def create(self, request: CapsuleRequest) -> CapsuleHandle:
        transport, uri = self._validate_settings(request)
        settings = request.settings
        session_id = self._validated_session_id(request.session_id)

        image = Path(settings.image).expanduser()
        if not settings.image or not image.is_file():
            raise CapsuleError(f"Capsule golden image not found: {settings.image!r}")
        image = image.resolve()

        # Validate deterministic caller-controlled values before storage is
        # allocated. Dynamic subnet occupancy is handled atomically below.
        pool = self._network_pool(settings.libvirt_network_cidr)
        vm_name, network_name, filter_name, bridge_name = self._resource_names(session_id)
        mac = self._mac_for(session_id)
        arch = self._host_arch(settings.libvirt_arch)

        # An explicit guest_address is a pin to this session's deterministic
        # first pool slot. Reject a mismatched pin before image inspection or
        # storage allocation; if that slot is occupied later, the under-lock
        # validation below refuses to silently move the pinned endpoint.
        if str(settings.guest_address or "").strip():
            pinned_network = next(self._candidate_networks(session_id, pool))
            self._validate_requested_address(
                settings.guest_address, str(pinned_network.network_address + 2)
            )

        base_format = self._base_image_format(image)

        # A collision is a hard boundary: never redefine or clean up a provider
        # object that existed before this allocation attempt.
        self._assert_resources_available(uri, vm_name, network_name, filter_name)

        root_parent = self._resolved_vm_root(settings.vm_root)
        try:
            root_parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            source = "configured vm_root" if settings.vm_root else "default system libvirt storage"
            raise CapsuleError(
                f"cannot prepare {source} at {root_parent}: {exc}. For qemu:///system, "
                "use a system-accessible directory writable by the Argus host process and "
                "traversable by the libvirt QEMU account."
            ) from exc
        root = root_parent / session_id
        if root.exists():
            raise CapsuleError(f"Capsule session directory already exists: {root}")
        try:
            root.mkdir(parents=False)
        except OSError as exc:
            raise CapsuleError(f"cannot create Capsule session directory {root}: {exc}") from exc

        overlay = root / "session.qcow2"
        network_xml = root / "network.xml"
        filter_xml = root / "nwfilter.xml"
        domain_xml = root / "domain.xml"
        owned_resources: set[str] = set()

        try:
            self._qemu_img_cmd(
                "create",
                "-f",
                "qcow2",
                "-F",
                base_format,
                "-b",
                str(image),
                str(overlay),
                timeout=45,
            )

            # The lock spans subnet selection through successful net-create.
            # Another Argus process therefore cannot observe the same subnet as
            # free before this network becomes visible in libvirt.
            with self._network_allocation_lock(uri):
                network = self._allocate_network(
                    uri, session_id, settings.libvirt_network_cidr
                )
                host_ip = str(network.network_address + 1)
                guest_ip = str(network.network_address + 2)
                self._validate_requested_address(settings.guest_address, guest_ip)

                self._write_xml(
                    network_xml,
                    self._network_xml(
                        network_name, bridge_name, network, mac, guest_ip, vm_name
                    ),
                )
                self._write_xml(
                    filter_xml,
                    self._filter_xml(
                        filter_name, host_ip, guest_ip, settings.guest_port
                    ),
                )
                self._write_xml(
                    domain_xml,
                    self._domain_xml(
                        vm_name,
                        overlay,
                        network_name,
                        filter_name,
                        mac,
                        settings.memory_mb,
                        settings.cpu_count,
                        arch,
                        str(settings.libvirt_machine or "").strip(),
                    ),
                )

                # Recheck object names, libvirt networks, and host/VPN routes
                # immediately before provider creation. The Argus lock closes
                # allocator races while the fresh host-route read prevents a
                # Capsule bridge from intentionally overriding an existing
                # physical or policy route.
                self._assert_resources_available(uri, vm_name, network_name, filter_name)
                if any(
                    network.overlaps(existing)
                    for existing in self._occupied_networks(uri)
                ):
                    raise CapsuleError(
                        f"libvirt Capsule subnet {network} became occupied during allocation"
                    )

                self._create_owned_resource(
                    uri,
                    "filter",
                    filter_name,
                    owned_resources,
                    "nwfilter-define",
                    filter_xml,
                    timeout=30,
                )
                self._create_owned_resource(
                    uri,
                    "network",
                    network_name,
                    owned_resources,
                    "net-create",
                    network_xml,
                    timeout=30,
                )

            # Domain definition/start no longer needs the subnet allocator lock:
            # the live libvirt network is now the durable reservation.
            self._create_owned_resource(
                uri,
                "domain",
                vm_name,
                owned_resources,
                "define",
                domain_xml,
                timeout=30,
            )
            self._virsh_cmd(uri, "start", vm_name, timeout=60)

            address = self._wait_for_guest_address(
                uri,
                vm_name,
                guest_ip,
                settings.boot_timeout_seconds,
            )
            return CapsuleHandle(
                session_id=session_id,
                provider=self.provider_name,
                vm_name=vm_name,
                root_dir=str(root),
                address=address,
                guest_port=settings.guest_port,
                transport=transport,
                guest_os="linux",
                architecture=arch,
            )
        except Exception as create_exc:
            preserve_storage = isinstance(create_exc, _AmbiguousResourceOwnership)
            cleanup_exc = self._cleanup_resources(
                uri,
                vm_name,
                network_name,
                filter_name,
                root,
                remove_storage=not preserve_storage,
                owned_resources=owned_resources,
                remove_nvram=arch == "aarch64",
            )
            if cleanup_exc is not None:
                raise CapsuleError(
                    "libvirt Capsule creation failed and partial cleanup also failed: "
                    f"create={create_exc}; cleanup={cleanup_exc}"
                ) from create_exc
            if preserve_storage:
                raise CapsuleError(
                    "libvirt Capsule creation has ambiguous provider ownership; "
                    f"session storage was preserved at {root}: {create_exc}"
                ) from create_exc
            raise

    def retain_failure(self, handle: CapsuleHandle, reason: str) -> FailureCapsule:
        if handle.provider != self.provider_name:
            raise CapsuleError(
                f"LibvirtProvider cannot retain handle owned by {handle.provider!r}"
            )
        root = Path(handle.root_dir)
        if not root.is_dir():
            raise CapsuleError(f"Capsule session storage is missing: {root}")

        # The provider is local-only and deterministic, so the URI is fixed by
        # the supported contract rather than persisted in potentially reportable
        # FailureCapsule metadata.
        uri = "qemu:///system"
        _vm_name, network_name, _filter_name, _bridge = self._resource_names(handle.session_id)
        state = self._virsh_cmd(uri, "domstate", handle.vm_name, timeout=15).strip().lower()
        if state not in {"shut off", "shutoff", "off"}:
            self._virsh_cmd(uri, "destroy", handle.vm_name, timeout=45)
        active_networks = self._list_names(
            self._virsh_cmd(uri, "net-list", "--name", timeout=15)
        )
        if network_name in active_networks:
            self._virsh_cmd(uri, "net-destroy", network_name, timeout=30)

        final_state = self._virsh_cmd(uri, "domstate", handle.vm_name, timeout=15).strip() or "shut off"
        failure = FailureCapsule(
            failure_id=handle.session_id,
            session_id=handle.session_id,
            provider=self.provider_name,
            vm_name=handle.vm_name,
            root_dir=str(root),
            reason=(reason or "test failure")[:2000],
            retained_at=datetime.now(timezone.utc).isoformat(),
            vm_state=final_state,
        )
        (root / "failure-capsule.json").write_text(
            json.dumps(failure.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return failure

    def destroy(self, handle: CapsuleHandle) -> None:
        if handle.provider != self.provider_name:
            raise CapsuleError(
                f"LibvirtProvider cannot destroy handle owned by {handle.provider!r}"
            )
        root = Path(handle.root_dir)
        _vm_name, network_name, filter_name, _bridge = self._resource_names(handle.session_id)
        cleanup_exc = self._cleanup_resources(
            "qemu:///system",
            handle.vm_name,
            network_name,
            filter_name,
            root,
            remove_storage=True,
            remove_nvram=handle.architecture == "aarch64",
        )
        if cleanup_exc is not None:
            raise cleanup_exc
