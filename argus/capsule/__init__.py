"""Disposable virtual-machine execution support for Argus."""

from argus.capsule.base import (
    CapsuleError,
    CapsuleHandle,
    CapsuleProvider,
    CapsuleRequest,
    CapsuleSettings,
    FailureCapsule,
)
from argus.capsule.guest import CapsuleGuestError, GuestAdapterProxy, GuestAgentClient

__all__ = [
    "CapsuleError",
    "CapsuleGuestError",
    "CapsuleHandle",
    "CapsuleProvider",
    "CapsuleRequest",
    "CapsuleSettings",
    "FailureCapsule",
    "GuestAdapterProxy",
    "GuestAgentClient",
]
