"""Windows Hyper-V provider for disposable Argus Capsules."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from argus.capsule.base import (
    CapsuleError,
    CapsuleHandle,
    CapsuleProvider,
    CapsuleRequest,
)


def _ps_quote(value: str) -> str:
    """Return a PowerShell single-quoted literal."""
    return "'" + str(value).replace("'", "''") + "'"


class HyperVProvider(CapsuleProvider):
    """Create a Generation-2 VM backed by a per-session differencing VHDX.

    The golden image is never attached directly. A session child disk and VM
    configuration live under ``vm_root/<session_id>`` and are removed on close.

    Argus reaches the guest agent over an existing Hyper-V virtual switch.
    Internal switches are accepted by default because the management OS can
    reach them without placing the guest directly on the physical LAN. External
    switches require explicit opt-in; private switches cannot carry host-to-
    guest HTTP traffic and are rejected.
    """

    provider_name = "hyperv"

    def __init__(
        self,
        *,
        runner: Optional[Callable[[str, float], str]] = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._runner = runner
        self._sleeper = sleeper
        self._clock = clock
        self._powershell: Optional[str] = None

    def _ensure_host(self) -> None:
        if self._runner is not None:
            return
        if sys.platform != "win32":
            raise CapsuleError("Hyper-V Capsules require a Windows host")
        self._powershell = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
        if not self._powershell:
            raise CapsuleError("PowerShell is required for the Hyper-V Capsule provider")
        self._run_ps("Get-Command New-VM -ErrorAction Stop | Out-Null; 'ok'", 15)

    def _run_ps(self, script: str, timeout_seconds: float = 30.0) -> str:
        if self._runner is not None:
            return str(self._runner(script, timeout_seconds)).strip()
        assert self._powershell is not None
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            completed = subprocess.run(
                [
                    self._powershell,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    script,
                ],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                creationflags=flags,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CapsuleError(f"Hyper-V PowerShell invocation failed: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown PowerShell error").strip()
            raise CapsuleError(f"Hyper-V command failed: {detail[:800]}")
        return completed.stdout.strip()

    def _validate_switch(self, name: str, allow_external: bool) -> None:
        if not name:
            raise CapsuleError(
                "capsule.switch_name is required; use a Hyper-V Internal switch "
                "that lets the host reach the guest agent"
            )
        switch_type = self._run_ps(
            "$s=Get-VMSwitch -Name " + _ps_quote(name) + " -ErrorAction Stop; "
            "$s.SwitchType.ToString()",
            15,
        ).strip()
        lowered = switch_type.lower()
        if lowered == "private":
            raise CapsuleError(
                f"Hyper-V switch {name!r} is Private; the host cannot reach the guest agent"
            )
        if lowered == "external" and not allow_external:
            raise CapsuleError(
                f"Hyper-V switch {name!r} is External; set allow_external_switch: true "
                "only if exposing the Capsule to that network is intentional"
            )
        if lowered not in {"internal", "external"}:
            raise CapsuleError(f"unsupported Hyper-V switch type for {name!r}: {switch_type!r}")

    def create(self, request: CapsuleRequest) -> CapsuleHandle:
        self._ensure_host()
        settings = request.settings
        if settings.provider.lower() != "hyperv":
            raise CapsuleError(f"HyperVProvider cannot handle provider {settings.provider!r}")
        if not settings.guest_token:
            raise CapsuleError(
                "Capsule guest token is missing; set the configured guest_token_env on the host"
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

        self._validate_switch(settings.switch_name, settings.allow_external_switch)

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
                "-AutomaticStartAction Nothing -AutomaticStopAction TurnOff; "
                f"Start-VM -Name {_ps_quote(vm_name)} | Out-Null",
                60,
            )

            address = settings.guest_address.strip() or self._wait_for_guest_address(
                vm_name, settings.boot_timeout_seconds
            )
            return CapsuleHandle(
                session_id=request.session_id,
                provider=self.provider_name,
                vm_name=vm_name,
                root_dir=str(root),
                address=address,
                guest_port=settings.guest_port,
            )
        except Exception as create_exc:
            cleanup_exc = self._cleanup_partial(vm_name, root)
            if cleanup_exc is not None:
                raise CapsuleError(
                    "Hyper-V Capsule creation failed and partial cleanup also failed: "
                    f"create={create_exc}; cleanup={cleanup_exc}"
                ) from create_exc
            raise

    def _wait_for_guest_address(self, vm_name: str, timeout_seconds: float) -> str:
        deadline = self._clock() + max(1.0, float(timeout_seconds))
        query = (
            "$ips=(Get-VMNetworkAdapter -VMName " + _ps_quote(vm_name) + ").IPAddresses; "
            "$ip=$ips | Where-Object { $_ -match '^([0-9]{1,3}\\.){3}[0-9]{1,3}$' "
            "-and $_ -notlike '169.254.*' } | Select-Object -First 1; "
            "if ($ip) { $ip }"
        )
        while self._clock() < deadline:
            address = self._run_ps(query, 15).strip()
            if address:
                return address.splitlines()[-1].strip()
            self._sleeper(1.0)
        raise CapsuleError(
            f"Capsule {vm_name!r} did not report an IPv4 address within "
            f"{timeout_seconds:.0f}s. Ensure Hyper-V Key-Value Pair Exchange is enabled "
            "in the golden image, or configure capsule.guest_address explicitly."
        )

    def _remove_vm(self, vm_name: str) -> None:
        self._run_ps(
            "$vm=Get-VM -Name " + _ps_quote(vm_name) + " -ErrorAction SilentlyContinue; "
            "if ($vm) { if ($vm.State -ne 'Off') { Stop-VM -VM $vm -TurnOff -Force }; "
            "Remove-VM -VM $vm -Force }",
            45,
        )

    def _cleanup_partial(self, vm_name: str, root: Path) -> Optional[Exception]:
        errors = []
        try:
            # Always query by name. New-VM may have succeeded even if the next
            # setup command failed before create() could record that fact.
            self._remove_vm(vm_name)
        except Exception as exc:
            errors.append(f"VM removal failed: {exc}")
        try:
            shutil.rmtree(root)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"session storage removal failed: {exc}")
        if errors:
            return CapsuleError("; ".join(errors))
        return None

    def destroy(self, handle: CapsuleHandle) -> None:
        if handle.provider != self.provider_name:
            raise CapsuleError(
                f"HyperVProvider cannot destroy handle owned by {handle.provider!r}"
            )
        root = Path(handle.root_dir)
        errors = []
        try:
            self._remove_vm(handle.vm_name)
        except Exception as exc:
            errors.append(f"VM removal failed: {exc}")
        try:
            shutil.rmtree(root)
        except FileNotFoundError:
            pass
        except OSError as exc:
            errors.append(f"session storage removal failed: {exc}")
        if errors:
            raise CapsuleError("; ".join(errors))
