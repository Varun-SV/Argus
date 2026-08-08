"""Execution environment abstraction for Argus.

An execution environment owns *where* a target runs. Adapters still own *how*
Argus observes and drives a target inside that environment.

PR2 deliberately ships only the local-host implementation. Future Capsule/VM
providers can implement this interface without changing the runner/roam/agent
callers that already consume the adapter-compatible surface.
"""

from __future__ import annotations

from abc import ABC
from dataclasses import dataclass

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
    Keeping that surface lets PR2 insert the execution-location boundary without
    a broad engine rewrite, while PR3 can replace the local implementation with
    a Capsule/guest-agent backed environment.
    """

    environment_type: str = "base"
    isolated: bool = False
    location: str = "unknown"

    def prepare(self) -> None:
        """Prepare resources required by this environment.

        Local execution needs no expensive setup. VM-backed implementations can
        create/restore a guest here before the target is launched.
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
        if not self._prepared:
            self.prepare()
        self.adapter.launch(target)

    def observe(self, include_screenshot: bool = True) -> Observation:
        obs = self.adapter.observe(include_screenshot=include_screenshot)
        # The environment is the model-facing execution boundary, so always
        # carry its current capabilities even when a raw/testing adapter did
        # not populate them itself.
        obs.action_capabilities = self.capabilities()
        return obs

    def capabilities(self) -> dict:
        return self.adapter.capabilities()

    def validate_action(self, action: dict) -> None:
        self.adapter.validate_action(action)

    def act(self, action: dict) -> str:
        # Public adapters created by Argus are PolicyAdapter-guarded, therefore
        # their ``act`` method crosses the existing validation/policy boundary.
        # Raw adapters supplied by tests/embedding code still get the same hard
        # boundary through ``execute``.
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
        # Compatibility escape hatch for existing UI code that inspects the
        # guarded adapter (for example safe-vs-physical Windows messaging).
        # Execution code should prefer the explicit environment surface above.
        return getattr(self.adapter, name)


def create_execution_environment(
    adapter_type: str,
    environment_type: str = "local",
) -> ExecutionEnvironment:
    """Create an execution environment for one Argus session.

    PR2 supports only ``local``. The explicit environment selector exists now
    so Capsule/VM providers can be added without changing the engine API.
    """
    kind = (environment_type or "local").lower().strip()
    if kind != "local":
        raise ExecutionEnvironmentError(
            f"unknown execution environment '{environment_type}' — available: local"
        )

    # Import from the implementation module, not ``argus.adapters``: the
    # package-level create_adapter is intentionally a backward-compatible alias
    # that now returns an ExecutionEnvironment.
    from argus.adapters.base import create_adapter as create_platform_adapter

    try:
        adapter = create_platform_adapter(adapter_type)
    except AdapterError as exc:
        raise ExecutionEnvironmentError(str(exc)) from exc
    return LocalExecutionEnvironment(adapter)
