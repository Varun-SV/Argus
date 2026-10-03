"""Verification entry points for user-supplied OS installation media."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from argus.provisioning.integrity import VerifiedFile, verify_regular_file
from argus.provisioning.model import EnvironmentDefinition, ProvisioningError


def verify_installation_media(
    definition: EnvironmentDefinition,
    *,
    allowed_roots: Iterable[str | Path] = (),
) -> VerifiedFile:
    source = definition.source
    if source.media_type != "iso":
        raise ProvisioningError(f"unsupported installation media type: {source.media_type!r}")
    if Path(source.path).suffix.lower() != ".iso":
        raise ProvisioningError("ISO installation media must use a .iso filename")
    return verify_regular_file(
        source.path,
        expected_sha256=source.sha256,
        allowed_roots=allowed_roots,
    )
