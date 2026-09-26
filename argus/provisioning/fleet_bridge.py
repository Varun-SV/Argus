"""Expose verified derived images through Fleet's existing inventory model."""

from __future__ import annotations

from pathlib import Path

from argus.fleet.heartbeat import ImageAdvertisement
from argus.provisioning.capsule_bridge import capsule_settings_from_derived_image
from argus.provisioning.model import DerivedImageManifest, EnvironmentDefinition


def derived_image_advertisement(
    definition: EnvironmentDefinition,
    manifest: DerivedImageManifest,
    image_path: str | Path,
    *,
    alias: str,
    guest_os: str,
) -> ImageAdvertisement:
    """Require the Capsule trust check before advertising image bytes to Fleet.

    Fleet's placement request uses ``digest`` as execution identity. ``alias``
    is solely an operator-facing label and cannot select mutable image content.
    """
    capsule_settings_from_derived_image(definition, manifest, image_path)
    return ImageAdvertisement(
        alias=alias,
        image_id=(f"{manifest.environment_id}:{manifest.provider}:"
                  f"{manifest.image_format}:{manifest.image_sha256}"),
        digest=f"sha256:{manifest.image_sha256}",
        guest_os=guest_os,
    )
