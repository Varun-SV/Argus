"""Argus Fleet execution-plane primitives."""

from .enrollment import (
    BOOTSTRAP_DIGEST_PROFILE,
    FLEET_REGISTRY_VERSION,
    BootstrapCredential,
    EnrollmentAcknowledgement,
    EnrollmentConflict,
    EnrollmentRequest,
    EnrollmentResult,
    FleetEnrollmentError,
    FleetEnrollmentRegistry,
)
from .identity import (
    FLEET_IDENTITY_VERSION,
    PRIVATE_KEY_FILE_VERSION,
    FleetIdentityError,
    NodeKeyPair,
    canonical_signed_message,
    public_key_fingerprint,
    verify_signature,
)

__all__ = [
    "BOOTSTRAP_DIGEST_PROFILE",
    "FLEET_IDENTITY_VERSION",
    "FLEET_REGISTRY_VERSION",
    "PRIVATE_KEY_FILE_VERSION",
    "BootstrapCredential",
    "EnrollmentAcknowledgement",
    "EnrollmentConflict",
    "EnrollmentRequest",
    "EnrollmentResult",
    "FleetEnrollmentError",
    "FleetEnrollmentRegistry",
    "FleetIdentityError",
    "NodeKeyPair",
    "canonical_signed_message",
    "public_key_fingerprint",
    "verify_signature",
]
