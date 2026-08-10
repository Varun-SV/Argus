"""Secure multi-provider Capsule execution environment."""

from __future__ import annotations

import secrets
import sys
from dataclasses import replace
from typing import Callable, Mapping, Optional

from argus.adapters.base import PolicyAdapter
from argus.capsule.base import (
    CapsuleHandle,
    CapsuleProvider,
    CapsuleProviderCapabilities,
    CapsuleRequest,
    CapsuleSettings,
)
from argus.capsule.guest import GuestAdapterProxy
from argus.capsule.secure_client import SecureGuestAgentClient
from argus.execution.base import ExecutionEnvironmentError
from argus.execution.capsule import CapsuleExecutionEnvironment


class SecureCapsuleExecutionEnvironment(CapsuleExecutionEnvironment):
    """Capsule environment with provider-enforced isolation and secure auth.

    PR7 keeps Hyper-V/Windows as the reference implementation and adds a Linux
    libvirt/QEMU provider behind the same lifecycle. Provider-specific security
    capabilities stay below this boundary; runner/agent code continues to see
    one Capsule execution environment.

    Secure retained Capsules intentionally reuse the disk/config-only retention
    path. Runtime bootstrap/TLS material has already been consumed, and no
    replacement control credential is persisted into a guest that may have been
    influenced by the application under test. Retention is forensic evidence
    preservation rather than a promise of remote restart.
    """

    def __init__(
        self,
        adapter_type: str,
        settings: CapsuleSettings,
        *,
        provider: Optional[CapsuleProvider] = None,
        client_factory: Callable[..., SecureGuestAgentClient] = SecureGuestAgentClient,
        session_id: Optional[str] = None,
    ) -> None:
        if not settings.rotate_session_token:
            raise ExecutionEnvironmentError(
                "secure Capsules require per-session bearer rotation; "
                "rotate_session_token cannot be disabled"
            )
        selected = provider or self._make_secure_provider(settings.provider)
        self._validate_provider_capabilities(selected, settings)
        super().__init__(
            adapter_type,
            settings,
            provider=selected,
            client_factory=client_factory,
            session_id=session_id,
        )
        self._session_token_rotated = False

    @staticmethod
    def _normalize_host_platform(value: str) -> str:
        raw = str(value or "").strip().lower()
        if raw.startswith("win") or raw == "windows":
            return "windows"
        if raw.startswith("linux"):
            return "linux"
        if raw in {"darwin", "mac", "macos", "osx"}:
            return "macos"
        return raw

    @staticmethod
    def _make_secure_provider(name: str, platform_name: str = "") -> CapsuleProvider:
        kind = (name or "auto").lower().strip()
        host = str(platform_name or sys.platform).lower()
        if kind == "auto":
            if host == "win32":
                kind = "hyperv"
            elif host.startswith("linux"):
                kind = "libvirt"
            else:
                raise ExecutionEnvironmentError(
                    "automatic Capsule provider selection currently supports Windows "
                    "(Hyper-V) and Linux (libvirt/QEMU) hosts"
                )

        if kind == "hyperv":
            from argus.capsule.hyperv_isolated import IsolatedHyperVProvider

            class SecureHyperVProvider(IsolatedHyperVProvider):
                provider_capabilities = CapsuleProviderCapabilities(
                    provider="hyperv",
                    host_platforms=("windows",),
                    guest_os=("windows",),
                    secure_transport=True,
                    network_isolation=True,
                    explicit_transfers=True,
                    failure_retention=True,
                    egress_allowlist=True,
                )

            return SecureHyperVProvider()
        if kind in {"libvirt", "qemu", "kvm"}:
            from argus.capsule.libvirt import LibvirtProvider

            return LibvirtProvider()
        raise ExecutionEnvironmentError(
            f"unknown Capsule provider {name!r} — available: auto, hyperv, libvirt"
        )

    @classmethod
    def _validate_provider_capabilities(
        cls,
        provider: CapsuleProvider,
        settings: CapsuleSettings,
    ) -> None:
        """Fail closed when an injected/extension provider weakens security."""
        capabilities = provider.capabilities()
        provider_name = str(provider.provider_name or "unknown")
        advertised = str(capabilities.provider or "").strip()
        if advertised and advertised != provider_name:
            raise ExecutionEnvironmentError(
                f"Capsule provider {provider_name!r} advertises mismatched capabilities "
                f"for {advertised!r}"
            )

        current_host = cls._normalize_host_platform(sys.platform)
        advertised_hosts = {
            cls._normalize_host_platform(item)
            for item in capabilities.host_platforms
            if str(item or "").strip()
        }
        if not advertised_hosts or current_host not in advertised_hosts:
            supported = ", ".join(sorted(advertised_hosts)) or "none"
            raise ExecutionEnvironmentError(
                f"Capsule provider {provider_name!r} does not support the current host "
                f"platform {current_host!r}; advertised hosts: {supported}"
            )

        missing: list[str] = []
        if not capabilities.secure_transport:
            missing.append("secure transport")
        if not capabilities.network_isolation:
            missing.append("network isolation")
        if not capabilities.explicit_transfers:
            missing.append("explicit staging/collection")
        if missing:
            raise ExecutionEnvironmentError(
                f"secure Capsule provider {provider_name!r} lacks required capability: "
                + ", ".join(missing)
            )

        if settings.retain_on_failure and not capabilities.failure_retention:
            raise ExecutionEnvironmentError(
                f"Capsule provider {provider_name!r} does not support requested failure retention"
            )

        network_mode = str(settings.network_mode or "host_only").strip().lower()
        wants_allowlist = network_mode == "allowlist" or bool(settings.egress_allowlist)
        if wants_allowlist and not capabilities.egress_allowlist:
            raise ExecutionEnvironmentError(
                f"Capsule provider {provider_name!r} does not support requested egress allowlisting"
            )

    @classmethod
    def from_mapping(
        cls,
        adapter_type: str,
        config: Optional[Mapping] = None,
    ) -> "SecureCapsuleExecutionEnvironment":
        return cls(adapter_type, CapsuleSettings.from_mapping(config))

    def _resolved_guest_os(self) -> str:
        configured = (self.settings.guest_os or "auto").lower().strip()
        capabilities = self.provider.capabilities()
        if configured == "auto":
            if capabilities.guest_os:
                return capabilities.guest_os[0]
            if self.provider.provider_name == "hyperv":
                return "windows"
            if self.provider.provider_name == "libvirt":
                return "linux"
            return "unknown"
        if capabilities.guest_os and not capabilities.supports_guest_os(configured):
            supported = ", ".join(capabilities.guest_os)
            raise ExecutionEnvironmentError(
                f"Capsule provider {self.provider.provider_name!r} does not support "
                f"guest_os={configured!r}; supported: {supported}"
            )
        return configured

    def prepare(self) -> None:
        if self._prepared:
            return
        handle: Optional[CapsuleHandle] = None
        try:
            guest_os = self._resolved_guest_os()
            # Resolve aliases before crossing the provider boundary. This keeps
            # existing Hyper-V/libvirt providers strict about their own names
            # while allowing user configuration to say provider: auto.
            request_settings = replace(
                self.settings,
                provider=self.provider.provider_name,
                guest_os=guest_os,
            )
            request = CapsuleRequest(
                session_id=self.session_id,
                adapter_type=self.type_name,
                settings=request_settings,
            )
            handle = self.provider.create(request)
            self._handle = handle
            client = self._client_factory(
                handle.endpoint,
                self.settings.guest_token,
                timeout_seconds=min(15.0, self.settings.agent_timeout_seconds),
                ca_cert_path=self.settings.guest_ca_cert,
                allow_insecure_http=self.settings.allow_insecure_http,
            )
            self._client = client
            client.wait_until_ready(self.settings.agent_timeout_seconds)

            rotate = getattr(client, "rotate_session_token", None)
            if not callable(rotate):
                raise ExecutionEnvironmentError(
                    "secure Capsule client does not support per-session bearer rotation"
                )
            session_token = secrets.token_urlsafe(48)
            rotate(self.session_id, session_token)
            self._session_token_rotated = True

            proxy = GuestAdapterProxy(
                client,
                adapter_type=self.type_name,
                input_mode=self.settings.guest_input_mode,
            )
            self._adapter = PolicyAdapter(proxy)
            self._prepared = True
        except Exception as prepare_exc:
            cleanup_exc = self._rollback_handle(handle)
            self._session_token_rotated = False
            if cleanup_exc is not None:
                raise ExecutionEnvironmentError(
                    "secure Capsule preparation failed and rollback also failed: "
                    f"prepare={prepare_exc}; cleanup={cleanup_exc}"
                ) from prepare_exc
            raise

    def _rollback_handle(self, handle: Optional[CapsuleHandle]):
        self._session_token_rotated = False
        return super()._rollback_handle(handle)

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._handle is None:
                self._session_token_rotated = False
