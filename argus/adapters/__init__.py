"""Target adapter API.

``create_adapter`` remains as a compatibility entry point, but session-level
callers now receive a LocalExecutionEnvironment. Low-level adapter
implementations still live under :mod:`argus.adapters.base` and are used by the
execution layer internally.
"""

from argus.adapters.base import Adapter, AdapterError, Observation, UIElement


def create_adapter(adapter_type: str):
    """Backward-compatible session factory returning a local environment.

    New code should prefer :func:`argus.execution.create_execution_environment`.
    Keeping this name prevents the CLI/GUI/dashboard and third-party callers
    from needing an all-at-once migration in PR2.
    """
    from argus.execution import create_execution_environment

    return create_execution_environment(adapter_type, environment_type="local")


__all__ = ["Adapter", "AdapterError", "Observation", "UIElement", "create_adapter"]
