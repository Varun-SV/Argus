"""ExecutionEnvironment backed by a disposable virtual-machine Capsule."""

from __future__ import annotations

import uuid
from typing import Callable, Mapping, Optional

from argus.adapters.base import Observation, PolicyAdapter
from argus.capsule.base import (
    CapsuleError,
    CapsuleHandle,
    CapsuleProvider,
    CapsuleRequest,
    CapsuleSettings,
    FailureCapsule,
)
from argus.capsule.guest import GuestAdapterProxy, GuestAgentClient
from argus.execution.base import (
    ExecutionEnvironment,
    ExecutionEnvironmentError,
    ExecutionEnvironmentInfo,
)


class CapsuleExecutionEnvironment(ExecutionEnvironment):
    """Run the target and all UI/CLI/browser input inside a disposable VM."""

    environment_type = "capsule"
    isolated = True
    location = "virtual-machine"

    def __init__(
        self,
        adapter_type: str,
        settings: CapsuleSettings,
        *,
        provider: Optional[CapsuleProvider] = None,
        client_factory: Callable[..., GuestAgentClient] = GuestAgentClient,
        session_id: Optional[str] = None,
    ) -> None:
        self.type_name = adapter_type
        self.settings = settings
        self.session_id = session_id or uuid.uuid4().hex
        self.provider = provider or self._make_provider(settings.provider)
        self._client_factory = client_factory
        self._handle: Optional[CapsuleHandle] = None
        self._client: Optional[GuestAgentClient] = None
        self._adapter: Optional[PolicyAdapter] = None
        self._prepared = False
        self._failure_reason = ""
        self._retained_failure: Optional[FailureCapsule] = None
        self._retention_error: Optional[dict] = None

    @staticmethod
    def _make_provider(name: str) -> CapsuleProvider:
        kind = (name or "hyperv").lower().strip()
        if kind == "hyperv":
            from argus.capsule.hyperv import HyperVProvider

            return HyperVProvider()
        raise ExecutionEnvironmentError(
            f"unknown Capsule provider {name!r} — available: hyperv"
        )

    @classmethod
    def from_mapping(
        cls,
        adapter_type: str,
        config: Optional[Mapping] = None,
    ) -> "CapsuleExecutionEnvironment":
        return cls(adapter_type, CapsuleSettings.from_mapping(config))

    def info(self) -> ExecutionEnvironmentInfo:
        return ExecutionEnvironmentInfo(
            environment_type=self.environment_type,
            adapter_type=self.type_name,
            isolated=True,
            location=self.provider.provider_name,
        )

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
            )
            self._client = client
            client.wait_until_ready(self.settings.agent_timeout_seconds)
            proxy = GuestAdapterProxy(
                client,
                adapter_type=self.type_name,
                input_mode=self.settings.guest_input_mode,
            )
            self._adapter = PolicyAdapter(proxy)
            self._prepared = True
        except Exception as prepare_exc:
            cleanup_exc = self._rollback_handle(handle)
            if cleanup_exc is not None:
                raise ExecutionEnvironmentError(
                    "Capsule preparation failed and rollback also failed: "
                    f"prepare={prepare_exc}; cleanup={cleanup_exc}"
                ) from prepare_exc
            raise

    def _rollback_handle(self, handle: Optional[CapsuleHandle]) -> Optional[Exception]:
        self._adapter = None
        self._client = None
        self._prepared = False
        if handle is None:
            self._handle = None
            return None
        try:
            self.provider.destroy(handle)
        except Exception as exc:
            # Retain the handle so launch()'s outer rollback or a later close()
            # can retry a transient provider cleanup failure.
            self._handle = handle
            return exc
        self._handle = None
        return None

    def launch(self, target: str) -> None:
        try:
            if not self._prepared:
                self.prepare()
            if self._adapter is None:
                raise ExecutionEnvironmentError("Capsule guest adapter was not prepared")
            self._adapter.launch(target)
        except Exception as launch_exc:
            try:
                self.close()
            except Exception as cleanup_exc:
                raise ExecutionEnvironmentError(
                    "Capsule target launch failed and rollback also failed: "
                    f"launch={launch_exc}; cleanup={cleanup_exc}"
                ) from launch_exc
            raise

    def observe(self, include_screenshot: bool = True) -> Observation:
        if self._adapter is None:
            raise ExecutionEnvironmentError("Capsule target is not launched")
        obs = self._adapter.observe(include_screenshot=include_screenshot)
        obs.action_capabilities = self.capabilities()
        return obs

    def capabilities(self) -> dict:
        if self._adapter is None:
            return {
                "actions": {"wait": {}, "done": {}},
                "notes": ["Capsule guest has not been prepared yet."],
            }
        return self._adapter.capabilities()

    def validate_action(self, action: dict) -> None:
        if self._adapter is None:
            raise ExecutionEnvironmentError("Capsule target is not launched")
        self._adapter.validate_action(action)

    def act(self, action: dict) -> str:
        if self._adapter is None:
            raise ExecutionEnvironmentError("Capsule target is not launched")
        return self._adapter.act(action)

    def record_failure(self, reason: str) -> None:
        if not self.settings.retain_on_failure:
            return
        if not self._failure_reason:
            self._failure_reason = (reason or "test failure")[:2000]

    def failure_capsule(self):
        if self._retained_failure is None:
            return None
        return self._retained_failure.to_dict()

    def failure_capsule_error(self):
        """Return structured recovery data when requested retention failed."""
        if self._retention_error is None:
            return None
        return dict(self._retention_error)

    def _retain_failure_before_teardown(self) -> bool:
        if not (
            self.settings.retain_on_failure
            and self._failure_reason
            and self._handle is not None
        ):
            return False
        handle = self._handle
        try:
            retained = self.provider.retain_failure(handle, self._failure_reason)
        except Exception as exc:
            # Never destroy evidence after a retention failure. Keep the handle
            # and expose exact recovery coordinates to the run result.
            self._handle = handle
            self._retention_error = {
                "status": "retention_failed",
                "session_id": handle.session_id,
                "provider": handle.provider,
                "vm_name": handle.vm_name,
                "root_dir": handle.root_dir,
                "error": str(exc),
                "recovery": (
                    "Capsule was preserved instead of destroyed. Inspect the registered VM "
                    "and session storage, then retry retention or clean it up manually."
                ),
            }
            raise CapsuleError(
                "Failure Capsule retention failed; Capsule preserved for recovery at "
                f"{handle.root_dir}: {exc}"
            ) from exc

        self._retained_failure = retained
        self._retention_error = None
        self._adapter = None
        self._client = None
        self._prepared = False
        self._handle = None
        return True

    def close(self) -> None:
        # Failure retention happens before guest-session close so the retained
        # disk represents the failure state before ordinary destructive cleanup.
        if self._retain_failure_before_teardown():
            return

        adapter = self._adapter
        handle = self._handle
        self._adapter = None
        self._client = None
        self._prepared = False

        errors = []
        if adapter is not None:
            try:
                adapter.close()
            except Exception as exc:
                errors.append(f"guest session close failed: {exc}")

        if handle is not None:
            try:
                self.provider.destroy(handle)
            except Exception as exc:
                # Keep the provider handle so a second close() can retry.
                self._handle = handle
                errors.append(f"Capsule destroy failed: {exc}")
            else:
                self._handle = None
        else:
            self._handle = None

        if errors:
            raise CapsuleError("; ".join(errors))
