"""OS installation-media provisioning primitives for Argus."""

from argus.provisioning.build import (
    load_published_derived_image,
    ProvisioningCleanupError,
    ProvisioningCleanupState,
)
from argus.provisioning.build_payload import BuildPayload, create_build_payload
from argus.provisioning.capsule_bridge import capsule_settings_from_derived_image
from argus.provisioning.evidence import AtesProvisioningRecorder, verify_provisioning_evidence
from argus.provisioning.fleet_bridge import derived_image_advertisement
from argus.provisioning.integrity import VerifiedFile, verify_regular_file
from argus.provisioning.media import verify_installation_media
from argus.provisioning.model import (
    DerivedImageManifest,
    EnvironmentDefinition,
    GuestRuntimeIdentity,
    InstallationMediaSource,
    InstallationSpec,
    MachineSpec,
    ProvisioningError,
)
from argus.provisioning.planner import (
    EnvironmentProvisioner,
    ProvisioningPlan,
    ProvisioningProviderCapabilities,
    ProvisioningResult,
    build_provisioning_plan,
    validate_provider_capabilities,
)
from argus.provisioning.providers import HyperVProvisioner, LibvirtProvisioner
from argus.provisioning.runtime_bundle import (
    GuestRuntimeBundleBuildResult,
    GuestRuntimeBundleManifest,
    VerifiedGuestRuntimeBundle,
    create_guest_runtime_bundle,
    verify_guest_runtime_bundle,
)
from argus.provisioning.spec import (
    environment_definition_from_mapping,
    load_environment_definition,
)

__all__ = [
    "load_published_derived_image",
    "AtesProvisioningRecorder",
    "BuildPayload",
    "DerivedImageManifest",
    "EnvironmentDefinition",
    "EnvironmentProvisioner",
    "GuestRuntimeBundleBuildResult",
    "GuestRuntimeBundleManifest",
    "GuestRuntimeIdentity",
    "InstallationMediaSource",
    "InstallationSpec",
    "HyperVProvisioner",
    "LibvirtProvisioner",
    "MachineSpec",
    "ProvisioningCleanupError",
    "ProvisioningCleanupState",
    "ProvisioningError",
    "ProvisioningPlan",
    "ProvisioningProviderCapabilities",
    "ProvisioningResult",
    "VerifiedFile",
    "VerifiedGuestRuntimeBundle",
    "build_provisioning_plan",
    "create_build_payload",
    "create_guest_runtime_bundle",
    "capsule_settings_from_derived_image",
    "derived_image_advertisement",
    "environment_definition_from_mapping",
    "load_environment_definition",
    "validate_provider_capabilities",
    "verify_guest_runtime_bundle",
    "verify_installation_media",
    "verify_provisioning_evidence",
    "verify_regular_file",
]
