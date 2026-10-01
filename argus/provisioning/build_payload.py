"""Private, non-secret build-payload staging for guest runtime installation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil

from argus.provisioning.model import (
    EnvironmentDefinition,
    GuestRuntimeIdentity,
    ProvisioningError,
)
from argus.provisioning.runtime_bundle import (
    GuestRuntimeBundleManifest,
    VerifiedGuestRuntimeBundle,
    verify_guest_runtime_bundle,
)


BUILD_PAYLOAD_SCHEMA_VERSION = "argus-build-payload-v1"


@dataclass(frozen=True)
class BuildPayload:
    root: Path
    manifest_path: Path
    runtime_bundle_path: Path
    runtime_identity: GuestRuntimeIdentity
    runtime_manifest: GuestRuntimeBundleManifest


def _copy_exact_bundle(
    identity: GuestRuntimeIdentity,
    destination: Path,
) -> None:
    # Verify the operator locator first, then re-verify the exact staged bytes.
    # A source swap between these operations can only produce a digest mismatch
    # in the private destination and therefore cannot reach a hypervisor.
    verify_guest_runtime_bundle(identity)
    with Path(identity.bundle_path).open("rb") as source, destination.open(
        "xb"
    ) as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)
        target.flush()
        os.fsync(target.fileno())


def create_build_payload(
    definition: EnvironmentDefinition,
    root: str | Path,
) -> BuildPayload:
    """Stage verified runtime bytes plus a secret-free build manifest."""
    identity = definition.require_guest_runtime()
    payload_root = Path(root)
    if payload_root.exists() or payload_root.is_symlink():
        raise ProvisioningError("build payload path already exists")
    payload_root.mkdir(mode=0o700, parents=False)

    runtime_bundle = payload_root / "runtime-bundle.zip"
    manifest_path = payload_root / "build-payload.json"
    try:
        _copy_exact_bundle(identity, runtime_bundle)
        verified: VerifiedGuestRuntimeBundle = verify_guest_runtime_bundle(
            identity,
            path=runtime_bundle,
            allowed_roots=(payload_root,),
        )
        manifest = {
            "schema_version": BUILD_PAYLOAD_SCHEMA_VERSION,
            "environment_id": definition.environment_id,
            "guest_runtime": identity.identity_dict(),
            "runtime_bundle_file": runtime_bundle.name,
            "runtime_content_sha256": verified.manifest.content_sha256,
        }
        with manifest_path.open("x", encoding="utf-8") as handle:
            json.dump(
                manifest,
                handle,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.flush()
            os.fsync(handle.fileno())
        runtime_bundle.chmod(0o444)
        manifest_path.chmod(0o444)
        return BuildPayload(
            root=payload_root.resolve(),
            manifest_path=manifest_path.resolve(),
            runtime_bundle_path=runtime_bundle.resolve(),
            runtime_identity=identity,
            runtime_manifest=verified.manifest,
        )
    except BaseException:
        for path in (manifest_path, runtime_bundle):
            if path.exists():
                path.chmod(0o600)
                path.unlink()
        if payload_root.exists():
            payload_root.rmdir()
        raise
