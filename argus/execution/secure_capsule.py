"""Secure multi-provider Capsule execution environment."""

from __future__ import annotations

import secrets
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping, Optional

from argus.adapters.base import PolicyAdapter
from argus.capsule.base import (
    CapsuleError,
    CapsuleHandle,
    CapsuleProvider,
    CapsuleProviderCapabilities,
    CapsuleRequest,
    CapsuleSettings,
)
from argus.capsule.bootstrap import (
    CapsuleBootstrapAttempt,
    create_bootstrap_attempt,
    create_bootstrap_iso,
)
from argus.capsule.control import (
    CapsuleControlRecord,
    CapsuleControlRegistry,
    CapsuleExecutionMode,
    CapsuleLifecycleState,
    new_capsule_id,
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
        injected_provider = provider is not None
        selected = provider or self._make_secure_provider(settings.provider)
        # Injected providers are an extension boundary, so their host-platform
        # claim is checked immediately. Built-in providers remain constructible
        # for config/factory inspection on non-native CI hosts, but are checked
        # again immediately before any provider allocation in prepare().
        self._validate_provider_capabilities(
            selected,
            settings,
            validate_host_platform=injected_provider,
        )
        super().__init__(
            adapter_type,
            settings,
            provider=selected,
            client_factory=client_factory,
            session_id=session_id,
        )
        self._session_token_rotated = False
        self._capsule_id = ""
        self._control_registry: CapsuleControlRegistry | None = None

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
    def _validate_provider_host_platform(cls, provider: CapsuleProvider) -> None:
        capabilities = provider.capabilities()
        provider_name = str(provider.provider_name or "unknown")
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

    @classmethod
    def _validate_provider_capabilities(
        cls,
        provider: CapsuleProvider,
        settings: CapsuleSettings,
        *,
        validate_host_platform: bool = True,
    ) -> None:
        """Fail closed when a provider weakens the secure Capsule contract."""
        capabilities = provider.capabilities()
        provider_name = str(provider.provider_name or "unknown")
        advertised = str(capabilities.provider or "").strip()
        if advertised and advertised != provider_name:
            raise ExecutionEnvironmentError(
                f"Capsule provider {provider_name!r} advertises mismatched capabilities "
                f"for {advertised!r}"
            )

        if validate_host_platform:
            cls._validate_provider_host_platform(provider)

        missing: list[str] = []
        if not capabilities.secure_transport:
            missing.append("secure transport")
        if not capabilities.network_isolation:
            missing.append("network isolation")
        if not capabilities.explicit_transfers:
            missing.append("explicit staging/collection")
        advertised_guest_os = tuple(
            str(item or "").strip().lower()
            for item in capabilities.guest_os
            if str(item or "").strip()
        )
        if not advertised_guest_os:
            missing.append("supported guest OS")
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
        supported = tuple(
            str(item or "").strip().lower()
            for item in capabilities.guest_os
            if str(item or "").strip()
        )
        if not supported:
            raise ExecutionEnvironmentError(
                f"Capsule provider {self.provider.provider_name!r} advertises no supported guest OS"
            )
        if configured == "auto":
            return supported[0]
        if configured not in supported:
            raise ExecutionEnvironmentError(
                f"Capsule provider {self.provider.provider_name!r} does not support "
                f"guest_os={configured!r}; supported: {', '.join(supported)}"
            )
        return configured

    def _is_provisioned(self) -> bool:
        commitments = (
            self.settings.environment_id,
            self.settings.base_image_sha256,
            self.settings.guest_runtime_identity,
        )
        if any(commitments) and not all(commitments):
            raise ExecutionEnvironmentError(
                "provisioned Capsule commitments must be supplied together"
            )
        return all(commitments)

    def _resolved_policy(self) -> tuple[str, tuple[str, ...]]:
        allowed = tuple(
            dict.fromkeys(
                str(value or "").strip().lower()
                for value in self.settings.allowed_execution_modes
            )
        )
        valid = {
            CapsuleExecutionMode.ISOLATED.value,
            CapsuleExecutionMode.SHARED_USER.value,
        }
        if not allowed or any(value not in valid for value in allowed):
            raise ExecutionEnvironmentError(
                "allowed_execution_modes must contain only isolated/shared_user"
            )
        default = str(self.settings.default_execution_mode or "").strip().lower()
        if default not in allowed:
            raise ExecutionEnvironmentError(
                "default_execution_mode must be in allowed_execution_modes"
            )
        return default, allowed

    def _request_settings(self) -> CapsuleSettings:
        return replace(
            self.settings,
            provider=self.provider.provider_name,
            guest_os=self._resolved_guest_os(),
        )

    def _validate_provisioned_security(self) -> None:
        if self.settings.guest_token or self.settings.guest_ca_cert:
            raise ExecutionEnvironmentError(
                "ISO-provisioned Capsules cannot use static guest_token or guest_ca_cert"
            )
        if (
            self.settings.guest_transport.lower().strip() != "https"
            or self.settings.allow_insecure_http
        ):
            raise ExecutionEnvironmentError(
                "ISO-provisioned Capsules require per-generation pinned HTTPS"
            )
        self._resolved_policy()

    def _new_generation_client(
        self,
        handle: CapsuleHandle,
        attempt: CapsuleBootstrapAttempt,
    ):
        client = self._client_factory(
            handle.endpoint,
            attempt.bootstrap_token,
            timeout_seconds=min(15.0, self.settings.agent_timeout_seconds),
            pinned_cert_sha256=attempt.tls_cert_sha256,
            allow_insecure_http=False,
        )
        client.wait_until_ready(self.settings.agent_timeout_seconds)
        health = client.health()
        expected = attempt.manifest
        if (
            health.get("ok") is not True
            or health.get("secure") is not True
            or health.get("capsule_id") != expected.capsule_id
            or int(health.get("control_generation") or 0)
            != expected.control_generation
            or health.get("execution_mode") != expected.execution_mode
            or health.get("runtime_identity") != expected.runtime_identity
        ):
            raise ExecutionEnvironmentError(
                "guest bootstrap identity does not match reserved Capsule generation"
            )
        rotate = getattr(client, "rotate_session_token", None)
        if not callable(rotate):
            raise ExecutionEnvironmentError(
                "secure Capsule client does not support generation-bound bearer rotation"
            )
        active_token = secrets.token_urlsafe(48)
        rotate(
            expected.session_id,
            active_token,
            capsule_id=expected.capsule_id,
            control_generation=expected.control_generation,
            execution_mode=expected.execution_mode,
        )
        return client

    @staticmethod
    def _destroy_bootstrap_material(
        attempt: CapsuleBootstrapAttempt,
        media: Path,
    ) -> None:
        media.unlink(missing_ok=True)
        attempt.destroy()

    def _establish_generation_locked(
        self,
        registry: CapsuleControlRegistry,
        handle: CapsuleHandle,
        request_settings: CapsuleSettings,
        *,
        execution_mode: str,
        preferred_session_id: str | None = None,
        attempts: int = 2,
    ) -> tuple[CapsuleHandle, object]:
        last_error: Exception | None = None
        for index in range(attempts):
            _record, generation = registry.reserve_generation(
                handle.capsule_id,
                lifecycle_state=CapsuleLifecycleState.BOOTSTRAPPING.value,
            )
            session_id = (
                preferred_session_id
                if index == 0 and preferred_session_id
                else uuid.uuid4().hex
            )
            attempt = create_bootstrap_attempt(
                self.settings.resolved_control_root / "bootstrap-attempts",
                capsule_id=handle.capsule_id,
                control_generation=generation,
                execution_mode=execution_mode,
                runtime_identity=self.settings.guest_runtime_identity,
                session_id=session_id,
            )
            media_dir = self.settings.resolved_control_root / "bootstrap-media"
            media_dir.mkdir(parents=True, exist_ok=True)
            media = media_dir / (
                f"{handle.capsule_id}-g{generation}-{uuid.uuid4().hex}.iso"
            )
            create_bootstrap_iso(attempt.root, media)
            attached = False
            started = False
            current = replace(
                handle,
                session_id=session_id,
                control_generation=generation,
                execution_mode=execution_mode,
                address="",
            )
            request = CapsuleRequest(
                session_id=session_id,
                adapter_type=self.type_name,
                settings=request_settings,
                capsule_id=handle.capsule_id,
                control_generation=generation,
                execution_mode=execution_mode,
            )
            try:
                self.provider.attach_bootstrap(current, media)
                attached = True
                current = self.provider.start_existing(current, request)
                started = True
                client = self._new_generation_client(current, attempt)
                registry.commit_generation(
                    current.capsule_id,
                    generation,
                    execution_mode=execution_mode,
                )
                try:
                    self.provider.detach_bootstrap(current, media)
                except Exception as detach_exc:
                    registry.transition(
                        current.capsule_id,
                        CapsuleLifecycleState.RECOVERY_REQUIRED.value,
                    )
                    try:
                        self.provider.quarantine(current)
                    except Exception:
                        pass
                    raise ExecutionEnvironmentError(
                        "Capsule generation committed but bootstrap detachment is uncertain"
                    ) from detach_exc
                self._destroy_bootstrap_material(attempt, media)
                return current, client
            except Exception as exc:
                last_error = exc
                if started:
                    try:
                        self.provider.stop_existing(current)
                    except Exception as stop_exc:
                        registry.transition(
                            current.capsule_id,
                            CapsuleLifecycleState.RECOVERY_REQUIRED.value,
                        )
                        raise ExecutionEnvironmentError(
                            "failed Capsule generation could not be stopped safely"
                        ) from stop_exc
                if attached:
                    try:
                        self.provider.detach_bootstrap(current, media)
                    except Exception as detach_exc:
                        registry.transition(
                            current.capsule_id,
                            CapsuleLifecycleState.RECOVERY_REQUIRED.value,
                        )
                        raise ExecutionEnvironmentError(
                            "failed Capsule generation has uncertain bootstrap detachment"
                        ) from detach_exc
                self._destroy_bootstrap_material(attempt, media)
                if index + 1 < attempts:
                    continue
                registry.transition(
                    current.capsule_id,
                    CapsuleLifecycleState.RECOVERY_REQUIRED.value,
                )
                break
        assert last_error is not None
        raise last_error

    def _prepare_provisioned(self) -> None:
        self._validate_provisioned_security()
        self._validate_provider_host_platform(self.provider)
        request_settings = self._request_settings()
        mode, _allowed = self._resolved_policy()
        registry = CapsuleControlRegistry(self.settings.resolved_control_root)
        capsule_id = new_capsule_id()
        self._control_registry = registry
        self._capsule_id = capsule_id
        handle: CapsuleHandle | None = None
        record_created = False
        with registry.operation_lock(capsule_id):
            try:
                allocate_request = CapsuleRequest(
                    session_id=self.session_id,
                    adapter_type=self.type_name,
                    settings=request_settings,
                    capsule_id=capsule_id,
                    execution_mode=mode,
                )
                handle = self.provider.create_stopped(allocate_request)
                if handle.capsule_id != capsule_id:
                    raise ExecutionEnvironmentError(
                        "provider returned a mismatched stable Capsule identity"
                    )
                if not handle.provider_resource_identity or not handle.mutable_disk_identity:
                    raise ExecutionEnvironmentError(
                        "provider did not return stable ownership identities"
                    )
                registry.create(
                    CapsuleControlRecord(
                        capsule_id=capsule_id,
                        provider=handle.provider,
                        provider_resource_identity=handle.provider_resource_identity,
                        mutable_disk_identity=handle.mutable_disk_identity,
                        environment_id=self.settings.environment_id,
                        base_image_sha256=self.settings.base_image_sha256,
                        effective_execution_mode=mode,
                        network_policy_identity=(
                            self.settings.network_mode
                            + ":"
                            + ",".join(self.settings.egress_allowlist)
                        ),
                    )
                )
                record_created = True
                handle, client = self._establish_generation_locked(
                    registry,
                    handle,
                    request_settings,
                    execution_mode=mode,
                    preferred_session_id=self.session_id,
                )
                self.session_id = handle.session_id
                self._handle = handle
                self._client = client
                self._adapter = PolicyAdapter(
                    GuestAdapterProxy(
                        client,
                        adapter_type=self.type_name,
                        input_mode=self.settings.guest_input_mode,
                    )
                )
                self._session_token_rotated = True
                self._prepared = True
            except Exception as prepare_exc:
                self._session_token_rotated = False
                if handle is not None:
                    try:
                        self.provider.destroy(handle)
                    except Exception as cleanup_exc:
                        if record_created:
                            registry.transition(
                                capsule_id,
                                CapsuleLifecycleState.RECOVERY_REQUIRED.value,
                            )
                        raise ExecutionEnvironmentError(
                            "provisioned Capsule preparation failed and destroy is uncertain: "
                            f"prepare={prepare_exc}; cleanup={cleanup_exc}"
                        ) from prepare_exc
                if record_created:
                    registry.transition(
                        capsule_id,
                        CapsuleLifecycleState.DESTROYED.value,
                    )
                raise

    def _prepare_legacy(self) -> None:
        handle: Optional[CapsuleHandle] = None
        try:
            self._validate_provider_host_platform(self.provider)
            request_settings = self._request_settings()
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
            self._adapter = PolicyAdapter(
                GuestAdapterProxy(
                    client,
                    adapter_type=self.type_name,
                    input_mode=self.settings.guest_input_mode,
                )
            )
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

    def prepare(self) -> None:
        if self._prepared:
            return
        if self._is_provisioned():
            self._prepare_provisioned()
        else:
            self._prepare_legacy()

    def _rollback_handle(self, handle: Optional[CapsuleHandle]):
        self._session_token_rotated = False
        return super()._rollback_handle(handle)

    def _mode_request(
        self,
        requested: str | None,
        *,
        requested_by_llm: bool,
        reconnect: bool,
    ) -> str:
        default, allowed = self._resolved_policy()
        mode = str(requested or default).strip().lower()
        if mode not in allowed:
            raise ExecutionEnvironmentError(
                f"execution mode {mode!r} is not permitted by user policy"
            )
        current = default
        if self._control_registry is not None and self._capsule_id:
            current = self._control_registry.load(
                self._capsule_id
            ).effective_execution_mode
        if (
            requested_by_llm
            and mode != current
            and not self.settings.allow_llm_mode_change
        ):
            raise ExecutionEnvironmentError(
                "LLM-requested Capsule mode change is disabled by user policy"
            )
        if (
            reconnect
            and requested_by_llm
            and not self.settings.failure_allow_llm_reconnect
        ):
            raise ExecutionEnvironmentError(
                "LLM-requested Failure Capsule reconnect is disabled by user policy"
            )
        return mode

    def _verify_retained_ownership(
        self,
        registry: CapsuleControlRegistry,
        handle: CapsuleHandle,
    ) -> None:
        record = registry.load(handle.capsule_id)
        resource_id, disk_id = self.provider.inspect_ownership(handle)
        if (
            resource_id != record.provider_resource_identity
            or disk_id != record.mutable_disk_identity
            or handle.provider != record.provider
        ):
            registry.transition(
                handle.capsule_id,
                CapsuleLifecycleState.RECOVERY_REQUIRED.value,
            )
            raise ExecutionEnvironmentError(
                "retained Capsule ownership no longer matches the durable host record"
            )

    def reconnect_failure(
        self,
        *,
        execution_mode: str | None = None,
        requested_by_llm: bool = False,
    ) -> None:
        if not self._is_provisioned():
            raise ExecutionEnvironmentError(
                "legacy static-token Failure Capsules do not support secure reconnect"
            )
        if not self.settings.failure_allow_reconnect:
            raise ExecutionEnvironmentError(
                "Failure Capsule reconnect is disabled by user policy"
            )
        failure = self._retained_failure
        if failure is None or not failure.capsule_id:
            raise ExecutionEnvironmentError(
                "no reconnectable Failure Capsule is retained"
            )
        mode = self._mode_request(
            execution_mode,
            requested_by_llm=requested_by_llm,
            reconnect=True,
        )
        registry = self._control_registry or CapsuleControlRegistry(
            self.settings.resolved_control_root
        )
        self._control_registry = registry
        self._capsule_id = failure.capsule_id
        handle = CapsuleHandle(
            session_id=failure.session_id,
            provider=failure.provider,
            vm_name=failure.vm_name,
            root_dir=failure.root_dir,
            address="",
            guest_port=self.settings.guest_port,
            transport="https",
            capsule_id=failure.capsule_id,
            control_generation=failure.failed_generation,
            execution_mode=failure.execution_mode,
            provider_resource_identity=failure.provider_resource_identity,
            mutable_disk_identity=failure.mutable_disk_identity,
        )
        with registry.operation_lock(handle.capsule_id):
            self._verify_retained_ownership(registry, handle)
            registry.transition(
                handle.capsule_id,
                CapsuleLifecycleState.RECONNECTING.value,
            )
            # A retained Capsule is normally powered off already. Calling stop
            # again is intentional: concurrent reconnect requests serialize on
            # the operation lock, and a later request must fence the generation
            # that an earlier request may just have activated.
            self.provider.stop_existing(handle)
            handle, client = self._establish_generation_locked(
                registry,
                handle,
                self._request_settings(),
                execution_mode=mode,
            )
            self.session_id = handle.session_id
            self._handle = handle
            self._client = client
            self._adapter = PolicyAdapter(
                GuestAdapterProxy(
                    client,
                    adapter_type=self.type_name,
                    input_mode=self.settings.guest_input_mode,
                )
            )
            self._prepared = True
            self._session_token_rotated = True
            self._retained_failure = None
            self._retention_error = None

    def transition_execution_mode(
        self,
        execution_mode: str,
        *,
        requested_by_llm: bool = False,
    ) -> None:
        if not self._is_provisioned() or self._handle is None:
            raise ExecutionEnvironmentError(
                "execution-mode transitions require an active provisioned Capsule"
            )
        mode = self._mode_request(
            execution_mode,
            requested_by_llm=requested_by_llm,
            reconnect=False,
        )
        registry = self._control_registry
        if registry is None:
            raise ExecutionEnvironmentError(
                "Capsule control registry is unavailable"
            )
        handle = self._handle
        with registry.operation_lock(handle.capsule_id):
            record = registry.load(handle.capsule_id)
            if mode == record.effective_execution_mode:
                return
            self._verify_retained_ownership(registry, handle)
            self.provider.stop_existing(handle)
            self._adapter = None
            self._client = None
            self._prepared = False
            handle, client = self._establish_generation_locked(
                registry,
                handle,
                self._request_settings(),
                execution_mode=mode,
            )
            self.session_id = handle.session_id
            self._handle = handle
            self._client = client
            self._adapter = PolicyAdapter(
                GuestAdapterProxy(
                    client,
                    adapter_type=self.type_name,
                    input_mode=self.settings.guest_input_mode,
                )
            )
            self._prepared = True
            self._session_token_rotated = True

    def _retain_failure_before_teardown(self) -> bool:
        if not self._is_provisioned():
            return super()._retain_failure_before_teardown()
        if not (
            self.settings.retain_on_failure
            and self._failure_reason
            and self._handle is not None
        ):
            return False
        registry = self._control_registry
        if registry is None:
            raise CapsuleError(
                "provisioned Capsule control registry is unavailable"
            )
        handle = self._handle
        with registry.operation_lock(handle.capsule_id):
            registry.transition(
                handle.capsule_id,
                CapsuleLifecycleState.QUARANTINING.value,
            )
            try:
                self.provider.quarantine(handle)
                retained = self.provider.retain_failure(
                    handle,
                    self._failure_reason,
                )
                registry.transition(
                    handle.capsule_id,
                    CapsuleLifecycleState.QUARANTINED.value,
                )
            except Exception:
                registry.transition(
                    handle.capsule_id,
                    CapsuleLifecycleState.RECOVERY_REQUIRED.value,
                )
                raise
        self._retained_failure = retained
        self._retention_error = None
        self._adapter = None
        self._client = None
        self._prepared = False
        self._workspace_ready = False
        self._staged_targets.clear()
        self._handle = None
        self._session_token_rotated = False
        return True

    def close(self) -> None:
        capsule_id = (
            self._handle.capsule_id
            if self._handle is not None
            else ""
        )
        try:
            super().close()
        finally:
            if self._handle is None:
                self._session_token_rotated = False
                if (
                    capsule_id
                    and self._control_registry is not None
                    and self._retained_failure is None
                ):
                    try:
                        self._control_registry.transition(
                            capsule_id,
                            CapsuleLifecycleState.DESTROYED.value,
                        )
                    except Exception:
                        pass
