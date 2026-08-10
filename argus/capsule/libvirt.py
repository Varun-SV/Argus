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
import platform
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
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
        if not self._virsh:
            raise CapsuleError("virsh is required for the libvirt Capsule provider")
        if not self._qemu_img:
            raise CapsuleError("qemu-img is required for the libvirt Capsule provider")
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
    def _resource_suffix(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:12]

    @classmethod
    def _resource_names(cls, session_id: str) -> tuple[str, str, str, str]:
        suffix = cls._resource_suffix(session_id)
        return (
            f"Argus-{session_id[:12]}",
            f"argus-{suffix}",
            f"argus-{suffix}-control",
            f"ar{suffix[:10]}",  # Linux interface names must fit IFNAMSIZ.
        )

    @staticmethod
    def _mac_for(session_id: str) -> str:
        digest = hashlib.sha256(("mac:" + session_id).encode("utf-8")).digest()
        return f"52:54:00:{digest[0]:02x}:{digest[1]:02x}:{digest[2]:02x}"

    @staticmethod
    def _default_network(session_id: str) -> ipaddress.IPv4Network:
        # 10.240.0.0/12 provides 4096 private /24 candidates. A session hash
        # makes simultaneous test collisions unlikely; a site can pin an
        # explicit CIDR when its host routing layout requires it.
        digest = hashlib.sha256(("network:" + session_id).encode("utf-8")).digest()
        slot = int.from_bytes(digest[:2], "big") % 4096
        second = 240 + (slot // 256)
        third = slot % 256
        return ipaddress.ip_network(f"10.{second}.{third}.0/24")

    @classmethod
    def _network_for(cls, session_id: str, configured: str) -> ipaddress.IPv4Network:
        if not str(configured or "").strip():
            return cls._default_network(session_id)
        try:
            network = ipaddress.ip_network(str(configured).strip(), strict=True)
        except ValueError as exc:
            raise CapsuleError(
                f"capsule.libvirt_network_cidr must be a canonical IPv4 network: {configured!r}"
            ) from exc
        if not isinstance(network, ipaddress.IPv4Network):
            raise CapsuleError("capsule.libvirt_network_cidr must be IPv4")
        if not network.is_private or network.is_link_local or network.is_multicast:
            raise CapsuleError("capsule.libvirt_network_cidr must be a private non-link-local network")
        if network.num_addresses < 4:
            raise CapsuleError("capsule.libvirt_network_cidr needs at least four IPv4 addresses")
        return network

    @staticmethod
    def _host_arch(configured: str) -> str:
        value = str(configured or platform.machine() or "").strip().lower()
        aliases = {
            "amd64": "x86_64",
            "x64": "x86_64",
            "arm64": "aarch64",
        }
        value = aliases.get(value, value)
        if value not in {"x86_64", "aarch64"}:
            raise CapsuleError(
                f"PR7 libvirt Capsules support same-architecture x86_64/aarch64 guests, got {value!r}"
            )
        return value

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

        os_node = ET.SubElement(root, "os")
        type_attrs = {"arch": arch}
        if machine:
            type_attrs["machine"] = machine
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
            normalized = str(ipaddress.ip_address(requested))
        except ValueError as exc:
            raise CapsuleError(f"capsule.guest_address must be a valid IPv4 address: {requested!r}") from exc
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

    def _cleanup_resources(
        self,
        uri: str,
        vm_name: str,
        network_name: str,
        filter_name: str,
        root: Path,
        *,
        remove_storage: bool,
    ) -> Optional[Exception]:
        try:
            domains = self._list_names(self._virsh_cmd(uri, "list", "--all", "--name", timeout=15))
            if vm_name in domains:
                active = self._list_names(self._virsh_cmd(uri, "list", "--name", timeout=15))
                if vm_name in active:
                    self._virsh_cmd(uri, "destroy", vm_name, timeout=45)
                self._virsh_cmd(uri, "undefine", vm_name, timeout=30)

            networks = self._list_names(self._virsh_cmd(uri, "net-list", "--all", "--name", timeout=15))
            if network_name in networks:
                active_networks = self._list_names(self._virsh_cmd(uri, "net-list", "--name", timeout=15))
                if network_name in active_networks:
                    self._virsh_cmd(uri, "net-destroy", network_name, timeout=30)

            filters = self._nwfilter_names(self._virsh_cmd(uri, "nwfilter-list", timeout=15))
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

        image = Path(settings.image).expanduser()
        if not settings.image or not image.is_file():
            raise CapsuleError(f"Capsule golden image not found: {settings.image!r}")
        image = image.resolve()

        root_parent = settings.resolved_vm_root
        root_parent.mkdir(parents=True, exist_ok=True)
        root = root_parent / request.session_id
        if root.exists():
            raise CapsuleError(f"Capsule session directory already exists: {root}")
        root.mkdir(parents=False)

        vm_name, network_name, filter_name, bridge_name = self._resource_names(request.session_id)
        overlay = root / "session.qcow2"
        network_xml = root / "network.xml"
        filter_xml = root / "nwfilter.xml"
        domain_xml = root / "domain.xml"

        network = self._network_for(request.session_id, settings.libvirt_network_cidr)
        host_ip = str(network.network_address + 1)
        guest_ip = str(network.network_address + 2)
        self._validate_requested_address(settings.guest_address, guest_ip)
        mac = self._mac_for(request.session_id)
        arch = self._host_arch(settings.libvirt_arch)

        try:
            base_format = self._base_image_format(image)
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

            self._write_xml(
                network_xml,
                self._network_xml(network_name, bridge_name, network, mac, guest_ip, vm_name),
            )
            self._write_xml(
                filter_xml,
                self._filter_xml(filter_name, host_ip, guest_ip, settings.guest_port),
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

            # Define all policy/resources before the first guest CPU executes.
            self._virsh_cmd(uri, "nwfilter-define", str(filter_xml), timeout=30)
            self._virsh_cmd(uri, "net-create", str(network_xml), timeout=30)
            self._virsh_cmd(uri, "define", str(domain_xml), timeout=30)
            self._virsh_cmd(uri, "start", vm_name, timeout=60)

            address = self._wait_for_guest_address(
                uri,
                vm_name,
                guest_ip,
                settings.boot_timeout_seconds,
            )
            return CapsuleHandle(
                session_id=request.session_id,
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
            cleanup_exc = self._cleanup_resources(
                uri,
                vm_name,
                network_name,
                filter_name,
                root,
                remove_storage=True,
            )
            if cleanup_exc is not None:
                raise CapsuleError(
                    "libvirt Capsule creation failed and partial cleanup also failed: "
                    f"create={create_exc}; cleanup={cleanup_exc}"
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
        active_networks = self._list_names(self._virsh_cmd(uri, "net-list", "--name", timeout=15))
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
        )
        if cleanup_exc is not None:
            raise cleanup_exc
