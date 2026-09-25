"""Bridge verified provisioned images back into the existing Capsule runtime."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from argus.capsule.base import CapsuleSettings
from argus.provisioning.integrity import verify_regular_file
from argus.provisioning.model import (
    DerivedImageManifest,
    EnvironmentDefinition,
    ProvisioningError,
)


def capsule_settings_from_derived_image(
    definition: EnvironmentDefinition,
    manifest: DerivedImageManifest,
    image_path: str | Path,
    *,
    settings: CapsuleSettings | None = None,
) -> CapsuleSettings:
    """Verify a derived base image and bind it to normal Capsule settings.

    This function is intentionally a bridge, not a new execution environment.
    After verification, the existing ExecutionEnvironment -> Capsule -> Adapter
    runtime remains authoritative.
    """

    manifest.validate_against(definition)
    candidate = Path(image_path)
    expected_suffix = {
        "vhdx": ".vhdx",
        "qcow2": ".qcow2",
        "raw": ".raw",
    }[manifest.image_format]
    if candidate.suffix.lower() != expected_suffix:
        raise ProvisioningError(
            f"derived {manifest.image_format} image must use {expected_suffix} suffix"
        )

    verified = verify_regular_file(
        candidate,
        expected_sha256=manifest.image_sha256,
    )
    if settings is None:
        base = CapsuleSettings(provider=manifest.provider)
    else:
        requested_provider = settings.provider.strip().lower()
        if requested_provider != manifest.provider:
            raise ProvisioningError(
                "derived image provider mismatch: "
                f"manifest requires {manifest.provider!r}, "
                f"Capsule settings request {settings.provider!r}"
            )
        base = replace(settings, provider=manifest.provider)
    return replace(base, image=str(verified.path))
