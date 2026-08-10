"""ExecutionEnvironment backed by a disposable virtual-machine Capsule."""

from __future__ import annotations

import os
import shlex
import uuid
from contextlib import ExitStack
from pathlib import Path
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
from argus.capsule.files import (
    TRANSFER_MAX_FILE_BYTES,
    enforce_total_bytes,
    guest_path_key,
    normalize_guest_relative_path,
    normalize_relative_path,
    workspace_path,
)
from argus.capsule.guest import GuestAdapterProxy, GuestAgentClient
from argus.capsule.safe_open import open_project_regular_file
from argus.capsule.safe_output import pin_artifact_tree
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
        self._workspace_ready = False
        self._staged_targets: dict[str, str] = {}
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
        self._workspace_ready = False
        self._staged_targets.clear()
        if handle is None:
            self._handle = None
            return None
        try:
            self.provider.destroy(handle)
        except Exception as exc:
            self._handle = handle
            return exc
        self._handle = None
        return None

    # ---- explicit staging / collection ---------------------------------

    def prepare_transfers(self) -> None:
        if not self._prepared:
            self.prepare()
        if self._client is None:
            raise ExecutionEnvironmentError("Capsule guest client was not prepared")
        if self._workspace_ready:
            return
        try:
            self._client.begin_files(self.session_id)
        except Exception as transfer_exc:
            try:
                self.close()
            except Exception as cleanup_exc:
                raise ExecutionEnvironmentError(
                    "Capsule transfer workspace preparation failed and rollback also failed: "
                    f"transfer={transfer_exc}; cleanup={cleanup_exc}"
                ) from transfer_exc
            raise
        self._workspace_ready = True

    def stage_files(self, entries, project_dir: Path) -> list[dict]:
        self.prepare_transfers()
        if self._client is None:
            raise ExecutionEnvironmentError("Capsule guest client was not prepared")

        try:
            # Bind every declared source before any upload begins. A watcher or
            # build process can replace the pathname later, but authority remains
            # attached to the already-open project-contained object.
            with ExitStack() as stack:
                prepared = []
                destinations = set()
                for entry in entries:
                    source_name = normalize_relative_path(
                        str(getattr(entry, "source", "") or "")
                    )
                    destination = normalize_guest_relative_path(
                        str(getattr(entry, "destination", "") or "")
                    )
                    destination_key = guest_path_key(destination)
                    if destination_key in destinations:
                        raise ExecutionEnvironmentError(
                            f"duplicate Capsule staging destination under Windows semantics: {destination}"
                        )
                    destinations.add(destination_key)

                    source_handle = stack.enter_context(
                        open_project_regular_file(project_dir, source_name)
                    )
                    size = int(os.fstat(source_handle.fileno()).st_size)
                    if size < 0 or size > TRANSFER_MAX_FILE_BYTES:
                        raise ExecutionEnvironmentError(
                            f"staging source exceeds {TRANSFER_MAX_FILE_BYTES} byte per-file limit: "
                            f"{source_name}"
                        )
                    prepared.append(
                        (entry, source_name, source_handle, destination, size)
                    )

                enforce_total_bytes(item[4] for item in prepared)

                staged = []
                for entry, source_name, source_handle, destination, _size in prepared:
                    stage_open = getattr(self._client, "stage_open_file", None)
                    if callable(stage_open):
                        data = stage_open(
                            source_handle,
                            source_name,
                            destination,
                            expected_sha256=str(getattr(entry, "sha256", "") or ""),
                        )
                    else:
                        # Compatibility for injected test clients. Production
                        # GuestAgentClient always uses the bound-handle method.
                        data = self._client.stage_file(
                            project_dir.joinpath(*source_name.split("/")),
                            destination,
                            expected_sha256=str(getattr(entry, "sha256", "") or ""),
                        )
                    self._staged_targets[guest_path_key(destination)] = str(data["guest_path"])
                    staged.append(
                        {
                            "source": str(getattr(entry, "source", "")),
                            "destination": destination,
                            "size": int(data["size"]),
                            "sha256": str(data["sha256"]),
                        }
                    )
                return staged
        except Exception as stage_exc:
            try:
                self.close()
            except Exception as cleanup_exc:
                raise ExecutionEnvironmentError(
                    "Capsule staging failed and rollback also failed: "
                    f"staging={stage_exc}; cleanup={cleanup_exc}"
                ) from stage_exc
            raise

    @staticmethod
    def _normalize_artifact_paths(paths) -> list[str]:
        normalized = []
        seen = set()
        for value in paths:
            relative = normalize_guest_relative_path(str(value or ""))
            key = guest_path_key(relative)
            if key in seen:
                raise ExecutionEnvironmentError(
                    f"duplicate Capsule artifact path under Windows semantics: {relative}"
                )
            seen.add(key)
            normalized.append(relative)
        return normalized

    def validate_artifact_paths(self, paths) -> list[str]:
        """Fail closed on invalid collection declarations before target launch."""
        return self._normalize_artifact_paths(paths)

    @staticmethod
    def _rollback_collected_artifacts(output_dir: Path, collected: list[dict]) -> list[str]:
        """Remove already committed artifacts when a later member of the set fails."""
        errors = []
        for item in reversed(collected):
            relative = str(item.get("path") or "")
            try:
                committed = workspace_path(output_dir, relative, must_exist=True)
                committed.unlink()
            except Exception as exc:
                errors.append(f"{relative}: {exc}")
        return errors

    def collect_artifacts(self, paths, output_dir: Path) -> list[dict]:
        if not self._workspace_ready or self._client is None:
            raise ExecutionEnvironmentError("Capsule artifact workspace is not available")
        normalized = self._normalize_artifact_paths(paths)

        # Pin the host runs/run/artifacts/parent directory tree for the entire
        # transaction. On the supported Windows Hyper-V host, these handles deny
        # delete/rename sharing so a validated run path cannot later become a
        # junction to an arbitrary host directory.
        with pin_artifact_tree(output_dir, normalized) as pinned_output:
            # The guest enforces the aggregate snapshot cap before reading each
            # artifact. Mirror the same running bound host-side so a bad/mismatched
            # guest cannot make the preflight set exceed policy unnoticed.
            infos = []
            preflight_sizes = []
            for relative in normalized:
                info = self._client.collect_info(relative)
                preflight_sizes.append(int(info["size"]))
                enforce_total_bytes(preflight_sizes)
                infos.append((relative, info))

            collected = []
            for relative, info in infos:
                try:
                    data = self._client.collect_file(relative, pinned_output, info=info)
                except Exception as exc:
                    rollback_errors = self._rollback_collected_artifacts(
                        pinned_output, collected
                    )
                    detail = f"Capsule artifact collection failed for {relative}: {exc}"
                    if rollback_errors:
                        detail += "; artifact rollback also failed: " + "; ".join(rollback_errors)
                    raise ExecutionEnvironmentError(detail) from exc
                collected.append(
                    {
                        "path": relative,
                        "size": int(data["size"]),
                        "sha256": str(data["sha256"]),
                        "host_path": f"artifacts/{relative}",
                    }
                )
            return collected

    # ---- target lifecycle ----------------------------------------------

    def launch(self, target: str) -> None:
        if not self._prepared:
            self.prepare()
        if self._adapter is None:
            raise ExecutionEnvironmentError("Capsule guest adapter was not prepared")

        target_attempted = False
        try:
            resolved_target = target
            staged_target = str(target).startswith("stage://")
            literal_gui_target = False
            if staged_target:
                relative = normalize_guest_relative_path(str(target)[len("stage://"):])
                resolved_target = self._staged_targets.get(guest_path_key(relative), "")
                if not resolved_target:
                    raise ExecutionEnvironmentError(
                        f"staged launch target was not declared/committed: {relative}"
                    )
                if self.type_name in {"cli", "terminal", "shell"}:
                    # CLIAdapter intentionally uses POSIX shlex parsing for command
                    # strings. Quote the staged executable so Windows backslashes
                    # survive that parser and become one subprocess argv element.
                    resolved_target = shlex.quote(resolved_target)
                elif self.type_name in {"desktop-gui", "desktop", "gui"}:
                    # Staged GUI executables are already one committed path.
                    # Never send them back through Windows command-string parsing.
                    literal_gui_target = True

            if literal_gui_target:
                launch_literal = getattr(self._adapter, "launch_literal", None)
                if not callable(launch_literal):
                    raise ExecutionEnvironmentError(
                        "Capsule guest adapter does not support literal staged GUI launches"
                    )
                target_attempted = True
                launch_literal(resolved_target)
            else:
                target_attempted = True
                self._adapter.launch(resolved_target)
        except Exception as launch_exc:
            # A missing/invalid stage reference is a pre-launch configuration
            # error and should roll back. Once the guest target was actually
            # attempted, PR4 retention semantics apply.
            if target_attempted and self.settings.retain_on_failure:
                self.record_failure(f"target launch failed: {launch_exc}")
            try:
                self.close()
            except Exception as cleanup_exc:
                action = (
                    "retention"
                    if target_attempted and self.settings.retain_on_failure
                    else "rollback"
                )
                raise ExecutionEnvironmentError(
                    f"Capsule target launch failed and {action} also failed: "
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
        self._workspace_ready = False
        self._staged_targets.clear()
        self._handle = None
        return True

    def close(self) -> None:
        if self._retain_failure_before_teardown():
            return

        adapter = self._adapter
        handle = self._handle
        errors = []

        if adapter is not None:
            try:
                adapter.close()
            except Exception as exc:
                guest_close_error = f"guest session close failed: {exc}"
                if self.settings.retain_on_failure and handle is not None:
                    self.record_failure(guest_close_error)
                    if self._retain_failure_before_teardown():
                        raise CapsuleError(guest_close_error) from exc
                errors.append(guest_close_error)

        self._adapter = None
        self._client = None
        self._prepared = False
        self._workspace_ready = False
        self._staged_targets.clear()

        if handle is not None:
            try:
                self.provider.destroy(handle)
            except Exception as exc:
                self._handle = handle
                errors.append(f"Capsule destroy failed: {exc}")
            else:
                self._handle = None
        else:
            self._handle = None

        if errors:
            raise CapsuleError("; ".join(errors))
