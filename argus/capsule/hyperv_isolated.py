"""PR6 Hyper-V provider with host-only default network isolation."""

from __future__ import annotations

import ipaddress
from pathlib import Path

from argus.capsule.base import CapsuleError, CapsuleHandle, CapsuleRequest
from argus.capsule.hyperv import HyperVProvider, _ps_quote


class IsolatedHyperVProvider(HyperVProvider):
    """Hyper-V provider that installs isolation before the VM is started.

    The virtual-switch ACL is authoritative from first boot packet onward:

    * host -> guest TCP control port is allowed statefully;
    * DHCP can be allowed explicitly for address bootstrap;
    * optional outbound CIDRs are stateful allow rules;
    * everything else is denied inbound and outbound.

    External switches remain rejected even with TLS. PR6's goal is to minimize
    authority, not to make broadly connected Capsule networking convenient.
    """

    def _validate_isolation_settings(self, request: CapsuleRequest) -> tuple[str, tuple[str, ...]]:
        settings = request.settings
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
                "plain HTTP Capsule control is disabled; set guest_transport=https "
                "or explicitly allow_insecure_http for disposable legacy development"
            )

        mode = (settings.network_mode or "host_only").lower().strip()
        if mode not in {"host_only", "allowlist"}:
            raise CapsuleError("capsule.network_mode must be host_only or allowlist")
        if mode == "host_only" and settings.egress_allowlist:
            raise CapsuleError(
                "egress_allowlist requires network_mode=allowlist; host_only permits no "
                "guest-initiated network destinations"
            )

        normalized = []
        for raw in settings.egress_allowlist:
            try:
                network = ipaddress.ip_network(str(raw).strip(), strict=False)
            except ValueError as exc:
                raise CapsuleError(f"invalid Capsule egress CIDR: {raw!r}") from exc
            if network.version != 4:
                raise CapsuleError("PR6 Hyper-V egress allowlist currently supports IPv4 CIDRs only")
            if network.is_multicast:
                raise CapsuleError(f"multicast egress is not permitted: {network}")
            value = str(network)
            if value not in normalized:
                normalized.append(value)
        return transport, tuple(normalized)

    def _host_switch_ipv4s(self, switch_name: str) -> list[str]:
        script = (
            "$s=Get-VMSwitch -Name " + _ps_quote(switch_name) + " -ErrorAction Stop; "
            "$alias='vEthernet ('+$s.Name+')'; "
            "Get-NetIPAddress -InterfaceAlias $alias -AddressFamily IPv4 -ErrorAction Stop | "
            "Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '0.0.0.0' } | "
            "Select-Object -ExpandProperty IPAddress"
        )
        raw = self._run_ps(script, 20)
        addresses = []
        for line in raw.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            try:
                address = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if address.version == 4 and not address.is_link_local and not address.is_unspecified:
                value = str(address)
                if value not in addresses:
                    addresses.append(value)
        if not addresses:
            raise CapsuleError(
                f"Hyper-V Internal switch {switch_name!r} has no usable management-OS IPv4 "
                "address; Argus cannot build a host-only control ACL"
            )
        return addresses

    def _apply_network_isolation(
        self,
        vm_name: str,
        switch_name: str,
        guest_port: int,
        *,
        egress_allowlist: tuple[str, ...],
        allow_dhcp: bool,
    ) -> None:
        host_ips = self._host_switch_ipv4s(switch_name)
        commands = [
            "$ErrorActionPreference='Stop'",
            f"$vm={_ps_quote(vm_name)}",
            "Get-VMNetworkAdapterExtendedAcl -VMName $vm -ErrorAction SilentlyContinue | "
            "Remove-VMNetworkAdapterExtendedAcl -ErrorAction Stop",
        ]

        weight = 1000
        for host_ip in host_ips:
            commands.append(
                "Add-VMNetworkAdapterExtendedAcl -VMName $vm -Action Allow "
                f"-Direction Inbound -RemoteIPAddress {_ps_quote(host_ip)} "
                f"-LocalPort {_ps_quote(str(int(guest_port)))} -Protocol TCP "
                f"-Weight {weight} -Stateful $true"
            )
            weight -= 1

        if allow_dhcp:
            commands.extend(
                [
                    "Add-VMNetworkAdapterExtendedAcl -VMName $vm -Action Allow "
                    "-Direction Outbound -LocalPort 68 -RemotePort 67 -Protocol UDP "
                    "-Weight 900 -Stateful $true",
                    "Add-VMNetworkAdapterExtendedAcl -VMName $vm -Action Allow "
                    "-Direction Inbound -LocalPort 68 -RemotePort 67 -Protocol UDP "
                    "-Weight 899",
                ]
            )

        weight = 800
        for network in egress_allowlist:
            commands.append(
                "Add-VMNetworkAdapterExtendedAcl -VMName $vm -Action Allow "
                f"-Direction Outbound -RemoteIPAddress {_ps_quote(network)} "
                f"-Weight {weight} -Stateful $true"
            )
            weight -= 1

        commands.extend(
            [
                "Add-VMNetworkAdapterExtendedAcl -VMName $vm -Action Deny "
                "-Direction Inbound -Weight 1",
                "Add-VMNetworkAdapterExtendedAcl -VMName $vm -Action Deny "
                "-Direction Outbound -Weight 1",
            ]
        )
        self._run_ps("; ".join(commands), 45)

    def _disable_host_file_copy(self, vm_name: str) -> None:
        # Fail closed if the expected integration service cannot be identified,
        # or if Hyper-V reports it still enabled after the disable command.
        self._run_ps(
            "$ErrorActionPreference='Stop'; "
            "$svc=Get-VMIntegrationService -VMName " + _ps_quote(vm_name) +
            " | Where-Object { $_.Name -eq 'Guest Service Interface' }; "
            "if (-not $svc) { throw 'Guest Service Interface integration service not found' }; "
            "if ($svc.Enabled) { Disable-VMIntegrationService -VMIntegrationService $svc -ErrorAction Stop }; "
            "$verify=Get-VMIntegrationService -VMName " + _ps_quote(vm_name) +
            " | Where-Object { $_.Name -eq 'Guest Service Interface' }; "
            "if (-not $verify -or $verify.Enabled) { throw 'Guest Service Interface remains enabled' }",
            20,
        )

    def create(self, request: CapsuleRequest) -> CapsuleHandle:
        self._ensure_host()
        settings = request.settings
        transport, egress_allowlist = self._validate_isolation_settings(request)

        if settings.provider.lower() != "hyperv":
            raise CapsuleError(f"IsolatedHyperVProvider cannot handle provider {settings.provider!r}")
        if not settings.guest_token:
            raise CapsuleError(
                "Capsule bootstrap token is missing; set ARGUS_CAPSULE_GUEST_TOKEN on the host"
            )
        if settings.allow_external_switch:
            raise CapsuleError(
                "External Hyper-V switches remain disabled in PR6; use an Internal switch "
                "plus an explicit egress allowlist"
            )
        if settings.guest_input_mode not in {"safe", "semantic", "physical", "legacy"}:
            raise CapsuleError("guest_input_mode must be safe/semantic or physical/legacy")
        if settings.memory_mb < 1024:
            raise CapsuleError("capsule.memory_mb must be at least 1024")
        if settings.cpu_count < 1:
            raise CapsuleError("capsule.cpu_count must be at least 1")
        if not (1 <= settings.guest_port <= 65535):
            raise CapsuleError("capsule.guest_port must be between 1 and 65535")

        image = Path(settings.image).expanduser()
        if not settings.image or not image.is_file():
            raise CapsuleError(f"Capsule golden image not found: {settings.image!r}")
        image = image.resolve()

        self._validate_switch(settings.switch_name)

        root_parent = settings.resolved_vm_root
        root_parent.mkdir(parents=True, exist_ok=True)
        root = root_parent / request.session_id
        if root.exists():
            raise CapsuleError(f"Capsule session directory already exists: {root}")
        root.mkdir(parents=False)

        vm_name = f"Argus-{request.session_id[:12]}"
        child_vhd = root / "session.vhdx"
        vm_path = root / "vm"
        try:
            self._run_ps(
                f"New-VHD -Path {_ps_quote(str(child_vhd))} "
                f"-ParentPath {_ps_quote(str(image))} -Differencing | Out-Null",
                45,
            )
            self._run_ps(
                "New-VM "
                f"-Name {_ps_quote(vm_name)} -Generation 2 "
                f"-MemoryStartupBytes {int(settings.memory_mb)}MB "
                f"-VHDPath {_ps_quote(str(child_vhd))} "
                f"-Path {_ps_quote(str(vm_path))} "
                f"-SwitchName {_ps_quote(settings.switch_name)} | Out-Null",
                45,
            )
            self._run_ps(
                "$ErrorActionPreference='Stop'; "
                f"Set-VMProcessor -VMName {_ps_quote(vm_name)} -Count {int(settings.cpu_count)}; "
                f"Set-VM -Name {_ps_quote(vm_name)} -AutomaticCheckpointsEnabled $false "
                "-AutomaticStartAction Nothing -AutomaticStopAction TurnOff",
                30,
            )

            if settings.disable_guest_file_copy:
                self._disable_host_file_copy(vm_name)
            self._apply_network_isolation(
                vm_name,
                settings.switch_name,
                settings.guest_port,
                egress_allowlist=egress_allowlist,
                allow_dhcp=settings.allow_dhcp,
            )

            self._run_ps(
                f"Start-VM -Name {_ps_quote(vm_name)} | Out-Null",
                60,
            )
            address = self._wait_for_guest_address(
                vm_name,
                settings.boot_timeout_seconds,
                requested_address=settings.guest_address,
            )
            return CapsuleHandle(
                session_id=request.session_id,
                provider=self.provider_name,
                vm_name=vm_name,
                root_dir=str(root),
                address=address,
                guest_port=settings.guest_port,
                transport=transport,
            )
        except Exception as create_exc:
            cleanup_exc = self._cleanup_partial(vm_name, root)
            if cleanup_exc is not None:
                raise CapsuleError(
                    "Hyper-V Capsule creation failed and partial cleanup also failed: "
                    f"create={create_exc}; cleanup={cleanup_exc}"
                ) from create_exc
            raise
