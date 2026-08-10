"""Disposable virtual-machine execution support for Argus."""

from argus.capsule.base import (
    CapsuleError,
    CapsuleHandle,
    CapsuleProvider,
    CapsuleProviderCapabilities,
    CapsuleRequest,
    CapsuleSettings,
    FailureCapsule,
)
from argus.capsule.guest import CapsuleGuestError, GuestAdapterProxy, GuestAgentClient
from argus.capsule.libvirt import LibvirtProvider

__all__ = [
    "CapsuleError",
    "CapsuleGuestError",
    "CapsuleHandle",
    "CapsuleProvider",
    "CapsuleProviderCapabilities",
    "CapsuleRequest",
    "CapsuleSettings",
    "FailureCapsule",
    "GuestAdapterProxy",
    "GuestAgentClient",
    "LibvirtProvider",
]
