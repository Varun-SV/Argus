"""Secure Capsule execution environment introduced by PR6."""

from __future__ import annotations

import json
import os
import secrets
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Callable, Mapping, Optional

from argus.adapters.base import PolicyAdapter
from argus.capsule.base import (
    CapsuleError,
    CapsuleHandle,
    CapsuleProvider,
    CapsuleRequest,
    CapsuleSettings,
)
from argus.capsule.guest import GuestAdapterProxy
from argus.capsule.hyperv_isolated import IsolatedHyperVProvider
from argus.capsule.secure_client import SecureGuestAgentClient
from argus.execution.base import ExecutionEnvironmentError
from argus.execution.capsule import CapsuleExecutionEnvironment


class SecureCapsuleExecutionEnvironment(CapsuleExecutionEnvironment):
    """Capsule environment with isolated networking and secure control auth."""

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
        self._recovery_token = ""
        self._recovery_credentials_path = ""
        self._recovery_armed = False

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
        self._recovery_token = ""
        self._recovery_credentials_path = ""
        self._recovery_armed = False
        return super()._rollback_handle(handle)

    @staticmethod
    def _recovery_file_for(handle: CapsuleHandle) -> Path:
        root = Path(handle.root_dir).resolve(strict=True)
        return root / "recovery-control.json"

    def _persist_recovery_control(self, handle: CapsuleHandle, token: str) -> str:
        """Persist the one-time restart bearer on the trusted host, not in reports."""
        destination = self._recovery_file_for(handle)
        if destination.is_symlink():
            raise CapsuleError("Failure Capsule recovery credential path cannot be a symlink")
        payload = {
            "version": 1,
            "session_id": handle.session_id,
            "vm_name": handle.vm_name,
            "address": handle.address,
            "guest_port": handle.guest_port,
            "transport": handle.transport,
            "token": token,
        }
        temp = destination.with_name(
            f".{destination.name}.argus-{uuid.uuid4().hex}.tmp"
        )
        try:
            with temp.open("x", encoding="utf-8") as stream:
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.chmod(temp, 0o600)
            except OSError:
                pass
            os.replace(temp, destination)
        except Exception:
            try:
                temp.unlink()
            except OSError:
                pass
            raise
        return str(destination)

    def _recovery_provisioning_failed(self, handle: CapsuleHandle, exc: Exception) -> None:
        self._handle = handle
        self._retention_error = {
            "status": "recovery_provision_failed",
            "session_id": handle.session_id,
            "provider": handle.provider,
            "vm_name": handle.vm_name,
            "root_dir": handle.root_dir,
            "error": str(exc),
            "recovery": (
                "Capsule was left powered/live rather than creating an unrestartable "
                "Failure Capsule. Fix recovery provisioning, then retry retention or "
                "clean up the VM manually."
            ),
        }

    def _arm_retained_recovery(self, handle: CapsuleHandle) -> None:
        if self._recovery_armed:
            return
        client = self._client
        if client is None or not self._session_token_rotated:
            raise CapsuleError("secure Capsule session auth is unavailable for retained recovery")
        arm = getattr(client, "arm_recovery", None)
        if not callable(arm):
            raise CapsuleError("secure Capsule client does not support retained recovery arming")

        token = self._recovery_token or secrets.token_urlsafe(48)
        path = self._recovery_credentials_path
        if not path:
            path = self._persist_recovery_control(handle, token)

        try:
            # The recovery credential is written into the guest only after the
            # application-under-test has been terminated. TurnOff would discard
            # its RAM anyway; this prevents the target from reading restart
            # material while preserving its on-disk failure state.
            client.close_session()
            arm(self.session_id, token)
        except Exception:
            if not self._recovery_armed:
                try:
                    Path(path).unlink()
                except OSError:
                    pass
            raise

        self._recovery_token = token
        self._recovery_credentials_path = path
        self._recovery_armed = True

    def _retain_failure_before_teardown(self) -> bool:
        if not (
            self.settings.retain_on_failure
            and self._failure_reason
            and self._handle is not None
        ):
            return False
        handle = self._handle

        if not self._recovery_armed:
            try:
                self._arm_retained_recovery(handle)
            except Exception as exc:
                self._recovery_provisioning_failed(handle, exc)
                raise CapsuleError(
                    "Failure Capsule recovery credentials could not be provisioned; "
                    f"Capsule preserved live at {handle.root_dir}: {exc}"
                ) from exc

        retained = super()._retain_failure_before_teardown()
        if retained and self._retained_failure is not None:
            self._retained_failure = replace(
                self._retained_failure,
                recovery_credentials_path=self._recovery_credentials_path,
            )
            # Keep the provider manifest and caller-visible metadata consistent,
            # but never include the recovery bearer itself in RunResult/reporting.
            manifest = Path(self._retained_failure.root_dir) / "failure-capsule.json"
            manifest.write_text(
                json.dumps(self._retained_failure.to_dict(), indent=2, sort_keys=True),
                encoding="utf-8",
            )
        return retained

    def close(self) -> None:
        try:
            super().close()
        finally:
            if self._handle is None:
                self._session_token_rotated = False
