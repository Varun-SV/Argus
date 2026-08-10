"""Secure Capsule execution environment introduced by PR6."""

from __future__ import annotations

import secrets
from typing import Callable, Mapping, Optional

from argus.adapters.base import PolicyAdapter
from argus.capsule.base import CapsuleHandle, CapsuleProvider, CapsuleRequest, CapsuleSettings
from argus.capsule.guest import GuestAdapterProxy
from argus.capsule.hyperv_isolated import IsolatedHyperVProvider
from argus.capsule.secure_client import SecureGuestAgentClient
from argus.execution.base import ExecutionEnvironmentError
from argus.execution.capsule import CapsuleExecutionEnvironment


class SecureCapsuleExecutionEnvironment(CapsuleExecutionEnvironment):
    """Capsule environment with isolated networking and secure control auth.

    Secure retained Capsules intentionally reuse the base PR4 disk/config-only
    retention path. Runtime bootstrap/TLS material has already been consumed,
    and no replacement control credential is persisted into a guest that may
    have been influenced by the application under test. Retention is therefore
    forensic evidence preservation rather than a promise of remote restart.
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
        super().__init__(
            adapter_type,
            settings,
            provider=provider or self._make_secure_provider(settings.provider),
            client_factory=client_factory,
            session_id=session_id,
        )
        self._session_token_rotated = False

    @staticmethod
    def _make_secure_provider(name: str) -> CapsuleProvider:
        kind = (name or "hyperv").lower().strip()
        if kind == "hyperv":
            return IsolatedHyperVProvider()
        raise ExecutionEnvironmentError(
            f"unknown Capsule provider {name!r} — available: hyperv"
        )

    @classmethod
    def from_mapping(
        cls,
        adapter_type: str,
        config: Optional[Mapping] = None,
    ) -> "SecureCapsuleExecutionEnvironment":
        return cls(adapter_type, CapsuleSettings.from_mapping(config))

    def prepare(self) -> None:
        if self._prepared:
            return
        handle: Optional[CapsuleHandle] = None
        try:
            request = CapsuleRequest(
                session_id=self.session_id,
                adapter_type=self.type_name,
                settings=self.settings,
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
