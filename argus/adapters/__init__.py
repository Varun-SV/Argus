"""Target adapter API.

``create_adapter`` is the session-level compatibility factory. It now honors the
project's configured execution location, returning either a local environment or
a disposable Capsule. Low-level platform adapter construction remains available
from :mod:`argus.adapters.base` for the execution layer and guest agent.
"""

from argus.adapters.base import Adapter, AdapterError, Observation, UIElement


def create_adapter(adapter_type: str):
    """Create the configured execution environment for ``adapter_type``."""
    from argus.config import load_config

    return load_config().make_execution_environment(adapter_type)


__all__ = ["Adapter", "AdapterError", "Observation", "UIElement", "create_adapter"]
