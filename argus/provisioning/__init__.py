"""OS installation-media provisioning primitives for Argus."""

from argus.provisioning.capsule_bridge import capsule_settings_from_derived_image
from argus.provisioning.integrity import VerifiedFile, verify_regular_file
from argus.provisioning.media import verify_installation_media
from argus.provisioning.model import (
    DerivedImageManifest,
    EnvironmentDefinition,
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
from argus.provisioning.spec import (
    environment_definition_from_mapping,
    load_environment_definition,
)

__all__ = [
    "DerivedImageManifest",
    "EnvironmentDefinition",
    "EnvironmentProvisioner",
    "InstallationMediaSource",
    "InstallationSpec",
    "MachineSpec",
    "ProvisioningError",
    "ProvisioningPlan",
    "ProvisioningProviderCapabilities",
    "ProvisioningResult",
    "VerifiedFile",
    "build_provisioning_plan",
    "capsule_settings_from_derived_image",
    "environment_definition_from_mapping",
    "load_environment_definition",
    "validate_provider_capabilities",
    "verify_installation_media",
    "verify_regular_file",
]
