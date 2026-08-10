"""Execution environment abstraction for Argus.

An execution environment owns *where* a target runs. Adapters still own *how*
Argus observes and drives a target inside that environment.

Local execution and disposable Capsule execution share this adapter-compatible
surface so runner/roam/agent code does not need hypervisor-specific branches.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from argus.adapters.base import Adapter, AdapterError, Observation


class ExecutionEnvironmentError(AdapterError):
    """Raised when an execution environment cannot be prepared or created.

    Subclassing :class:`AdapterError` keeps existing callers backward-compatible
    while giving environment failures a distinct type for newer code.
    """


@dataclass(frozen=True)
class ExecutionEnvironmentInfo:
    """Stable metadata describing where an Argus session executes."""

    environment_type: str
    adapter_type: str
    isolated: bool
    location: str


class ExecutionEnvironment(Adapter, ABC):
    """Adapter-compatible boundary around an execution location.

    The compatibility with :class:`Adapter` is intentional: current Argus
    engines already consume ``launch / observe / act / close / capabilities``.
    Keeping that surface lets the execution-location boundary wrap either the
    local host or a Capsule/guest-agent implementation without rewriting the
    agent loop.

    Lifecycle contract
    ------------------
    The environment owns every resource acquired by :meth:`prepare`. A public
    launch path must be exception-safe. If preparation succeeds (even only
    partially) but target launch fails, the environment must release those
    resources before propagating the failure unless an explicit retention
    policy preserves them for diagnostics. If requested preservation itself
    fails, implementations must fail safe by keeping recoverable resources
    intact and surfacing enough recovery metadata for an operator to inspect or
    clean them up. Implementations whose ``prepare`` method can be called
    directly must still roll back any partial allocation before raising, because
    no valid test state necessarily exists yet to retain. This prevents
    VM/disk/network allocations from being leaked accidentally while allowing
    deliberately retained failure state to remain available for reproduction.

    File-transfer contract
    ----------------------
    Staging and collection are explicit execution-environment operations. Local
    execution does not implicitly copy files. Capsule implementations may expose
    a bounded per-session workspace; paths crossing that boundary must be
    deterministic policy inputs rather than agent/model-generated destinations.
    Transfer preparation/staging occurs before target launch, while collection
    must finish before the environment is closed, retained, or destroyed.
    """

    environment_type: str = "base"
    isolated: bool = False
    location: str = "unknown"

    def prepare(self) -> None:
        """Prepare resources required by this environment.

        Implementations must make direct calls exception-safe: if preparation
        raises after partially allocating resources, it must release those
        partial resources before propagating the exception. The normal
        :meth:`launch` path should additionally roll back the whole environment
        when preparation fails, and should release target-launch resources unless
        an explicit retention policy preserves the prepared failure state (or
        fails safe while preserving recoverable resources for operator action).
        """

    def info(self) -> ExecutionEnvironmentInfo:
        return ExecutionEnvironmentInfo(
            environment_type=self.environment_type,
            adapter_type=self.type_name,
            isolated=self.isolated,
            location=self.location,
        )

    def describe_environment(self) -> str:
        info = self.info()
        isolation = "isolated" if info.isolated else "shared"
        return f"{info.environment_type}:{info.adapter_type} ({info.location}, {isolation})"

    def record_failure(self, reason: str) -> None:
        """Record a failure before teardown.

        Local environments intentionally ignore this. VM-backed environments may
        use it to retain reproducible failure state when explicitly configured.
        """

    def failure_capsule(self):
        """Return retained failure metadata, if this environment produced any."""
        return None

    def failure_capsule_error(self):
        """Return retention recovery metadata, if retention itself failed."""
        return None

    def prepare_transfers(self) -> None:
        """Prepare an explicit staging/collection workspace."""
        raise ExecutionEnvironmentError(
            "file staging/collection is only supported by a transfer-capable execution environment"
        )

    def stage_files(self, entries, project_dir: Path) -> list[dict]:
        """Stage explicit project files into the execution workspace."""
        raise ExecutionEnvironmentError(
            "file staging is not supported by this execution environment"
        )

    def collect_artifacts(self, paths, output_dir: Path) -> list[dict]:
        """Collect explicit workspace files into ``output_dir``."""
        raise ExecutionEnvironmentError(
            "artifact collection is not supported by this execution environment"
        )


class LocalExecutionEnvironment(ExecutionEnvironment):
    """Run a guarded Argus adapter directly on the current host/session."""

    environment_type = "local"
    isolated = False
    location = "host"

    def __init__(self, adapter: Adapter) -> None:
        self.adapter = adapter
        self.type_name = adapter.type_name
        self._prepared = False

    def prepare(self) -> None:
        self._prepared = True

    def launch(self, target: str) -> None:
        """Prepare and launch atomically from the caller's point of view."""
        try:
            if not self._prepared:
                self.prepare()
            self.adapter.launch(target)
        except Exception as launch_exc:
            try:
                self.close()
            except Exception as cleanup_exc:
                raise ExecutionEnvironmentError(
                    "environment preparation/target launch failed and rollback "
                    f"also failed: launch={launch_exc}; cleanup={cleanup_exc}"
                ) from launch_exc
            raise

    def observe(self, include_screenshot: bool = True) -> Observation:
        obs = self.adapter.observe(include_screenshot=include_screenshot)
        obs.action_capabilities = self.capabilities()
        return obs

    def capabilities(self) -> dict:
        return self.adapter.capabilities()

    def validate_action(self, action: dict) -> None:
        self.adapter.validate_action(action)

    def act(self, action: dict) -> str:
        from argus.adapters.base import PolicyAdapter

        if isinstance(self.adapter, PolicyAdapter):
            return self.adapter.act(action)
        return self.adapter.execute(action)

    def close(self) -> None:
        try:
            self.adapter.close()
        finally:
            self._prepared = False

    def __getattr__(self, name):
        return getattr(self.adapter, name)


def create_execution_environment(
    adapter_type: str,
    environment_type: str = "local",
    capsule_config: Optional[Mapping] = None,
) -> ExecutionEnvironment:
    """Create the requested session execution location.

    ``local`` preserves the current host behavior. ``capsule`` creates a
    disposable VM-backed environment; PR3 initially supports Hyper-V through
    its Capsule provider configuration.
    """
    kind = (environment_type or "local").lower().strip()
    if kind == "capsule":
        from argus.execution.capsule import CapsuleExecutionEnvironment

        return CapsuleExecutionEnvironment.from_mapping(adapter_type, capsule_config)
    if kind != "local":
        raise ExecutionEnvironmentError(
            f"unknown execution environment '{environment_type}' — available: local, capsule"
        )

    from argus.adapters.base import create_adapter as create_platform_adapter

    try:
        adapter = create_platform_adapter(adapter_type)
    except AdapterError as exc:
        raise ExecutionEnvironmentError(str(exc)) from exc
    return LocalExecutionEnvironment(adapter)
