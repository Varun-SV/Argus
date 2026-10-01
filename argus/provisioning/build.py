"""Transactional publication of installer-produced Capsule base images.

The cache root is operator controlled. The lock serializes Argus builders for a
single immutable (environment, provider, format) key; a failed build never
replaces an already published image.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterator

from argus.ates import RunId
from argus.ates.store import _run_directory_key
from argus.provisioning.build_payload import BuildPayload, create_build_payload
from argus.provisioning.evidence import AtesProvisioningRecorder, verify_provisioning_evidence
from argus.provisioning.integrity import verify_regular_file
from argus.provisioning.media import verify_installation_media
from argus.provisioning.model import DerivedImageManifest, EnvironmentDefinition, ProvisioningError
from argus.provisioning.planner import ProvisioningPlan, ProvisioningResult


class ProvisioningCleanupError(ProvisioningError):
    """A provider could not prove its temporary VM was removed."""


def _evidence_root(plan: ProvisioningPlan) -> Path:
    return plan.evidence_root or plan.cache_dir.parents[2]


def _evidence_run_dir(plan: ProvisioningPlan, run_id: str) -> Path:
    return _evidence_root(plan) / ".argus" / "runs" / _run_directory_key(RunId(run_id))


def _remove_private_dir(path: Path) -> None:
    """Remove a failed unpublished build, including read-only image bytes on Windows."""
    def writable_remove(func, name, _exc) -> None:
        target = Path(name)
        target.chmod(0o700 if target.is_dir() else 0o600)
        func(name)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=writable_remove)
    else:
        shutil.rmtree(path, onerror=writable_remove)


@contextmanager
def _build_lock(path: Path) -> Iterator[None]:
    """Hold a kernel lock, automatically released even after process termination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == b"":
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


def _published(definition: EnvironmentDefinition, plan: ProvisioningPlan) -> ProvisioningResult:
    try:
        with plan.manifest_path.open("r", encoding="utf-8") as handle:
            manifest = DerivedImageManifest.from_mapping(json.load(handle))
    except (OSError, ValueError, TypeError) as exc:
        raise ProvisioningError("published derived-image manifest is invalid") from exc
    manifest.validate_against(definition)
    if manifest.provider != plan.provider or manifest.image_format != plan.output_format:
        raise ProvisioningError("published derived-image provider or format conflicts with plan")
    verify_regular_file(plan.image_path, expected_sha256=manifest.image_sha256,
                        allowed_roots=(plan.cache_dir,))
    if manifest.evidence_run_id is None:
        raise ProvisioningError("published derived image lacks canonical ATES evidence")
    verify_provisioning_evidence(
        _evidence_run_dir(plan, manifest.evidence_run_id), definition, plan,
        manifest.image_sha256,
    )
    return ProvisioningResult(plan, manifest)


def _copy_verified_media(definition: EnvironmentDefinition, destination: Path) -> None:
    # First check the configured locator, then hash the exact bytes copied into
    # our private work directory. The hypervisor sees only the staged pathname.
    verify_installation_media(definition)
    with Path(definition.source.path).open("rb") as source, destination.open("xb") as target:
        shutil.copyfileobj(source, target, length=1024 * 1024)
        target.flush()
        os.fsync(target.fileno())
    verify_regular_file(destination, expected_sha256=definition.source.sha256,
                        allowed_roots=(destination.parent,))


def publish_derived_image(
    definition: EnvironmentDefinition,
    plan: ProvisioningPlan,
    install: Callable[[Path, Path, BuildPayload], None],
    *,
    validate_baseline: Callable[[Path], None] | None = None,
) -> ProvisioningResult:
    """Stage media, build privately, then publish image and manifest together.

    ``install(iso, image, build_payload)`` must return only after the installer VM has shut
    down and all provider-owned mutable resources have been destroyed. It must
    clean those resources in a ``finally`` block even on cancellation.
    The baseline check must boot a disposable child of the candidate image and
    prove that the expected guest OS and secure Argus agent are ready. A disk
    format check or installer shutdown alone cannot authorize publication.
    """
    if validate_baseline is None:
        raise ProvisioningError("baseline Capsule validation is required before publication")
    if (plan.environment_id, plan.definition_sha256) != (
        definition.environment_id, definition.definition_sha256
    ):
        raise ProvisioningError("plan does not match environment definition")
    if plan.image_path != plan.cache_dir / f"base.{plan.output_format}" or (
        plan.manifest_path != plan.cache_dir / "manifest.json"
    ):
        raise ProvisioningError("plan publication paths are inconsistent")

    lock_path = plan.cache_dir.parent / f".{plan.output_format}.lock"
    with _build_lock(lock_path):
        if plan.cache_dir.exists() or plan.cache_dir.is_symlink():
            return _published(definition, plan)

        workspace = Path(tempfile.mkdtemp(prefix=".building-", dir=plan.cache_dir.parent))
        preserve_workspace = False
        published_here = False
        recorder: AtesProvisioningRecorder | None = None
        try:
            recorder = AtesProvisioningRecorder(_evidence_root(plan), definition, plan)
            staged_iso = workspace / "installation.iso"
            image = workspace / plan.image_path.name
            recorder.begin_stage("media_verified")
            _copy_verified_media(definition, staged_iso)
            build_payload = create_build_payload(
                definition, workspace / "build-payload"
            )
            recorder.complete_stage("media_verified")
            recorder.begin_stage("provider_selected")
            recorder.complete_stage("provider_selected")
            recorder.begin_stage("installation")
            install(staged_iso, image, build_payload)
            if not image.is_file() or image.is_symlink():
                raise ProvisioningError("installer did not produce a regular base image")
            recorder.complete_stage("installation")
            recorder.begin_stage("baseline_validated")
            validate_baseline(image)
            recorder.complete_stage("baseline_validated")
            recorder.begin_stage("image_hashed")
            digest = sha256()
            with image.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            if image.stat().st_size == 0:
                raise ProvisioningError("installer produced an empty base image")
            verify_regular_file(image, expected_sha256=digest.hexdigest(),
                                allowed_roots=(workspace,))
            recorder.complete_stage("image_hashed", image_sha256=digest.hexdigest())
            manifest = DerivedImageManifest(
                environment_id=definition.environment_id,
                definition_sha256=definition.definition_sha256,
                source_sha256=definition.source.sha256,
                provider=plan.provider,
                image_format=plan.output_format,
                image_sha256=digest.hexdigest(),
                architecture=definition.machine.architecture,
                created_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                evidence_run_id=str(recorder.run_id),
            )
            staged_iso.unlink()
            _remove_private_dir(build_payload.root)
            with (workspace / "manifest.json").open("x", encoding="utf-8") as handle:
                json.dump(asdict(manifest), handle, sort_keys=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            image.chmod(0o444)
            recorder.begin_stage("publication")
            # The lock protects cooperating builders. A foreign cache entry is
            # never overwritten, even when it appears during installation.
            if plan.cache_dir.exists() or plan.cache_dir.is_symlink():
                raise ProvisioningError("derived-image cache was published concurrently")
            workspace.rename(plan.cache_dir)
            published_here = True
            recorder.complete_stage("publication")
            recorder.finish()
            # ATES verification reacquires the run authority; release the
            # producer's handle first (especially required by Windows locks).
            recorder.close()
            verify_provisioning_evidence(
                recorder.run_dir, definition, plan, manifest.image_sha256,
            )
            return ProvisioningResult(plan, manifest)
        except ProvisioningCleanupError as exc:
            preserve_workspace = True
            if recorder is not None:
                try:
                    recorder.abort(cleanup_confirmed=False)
                except Exception:
                    pass
            raise ProvisioningCleanupError(
                f"provisioning VM cleanup is uncertain; private workspace retained at {workspace}"
            ) from exc
        except BaseException:
            if published_here:
                _remove_private_dir(plan.cache_dir)
            if recorder is not None:
                try:
                    recorder.abort(cleanup_confirmed=True)
                except Exception:
                    pass
            raise
        finally:
            if recorder is not None:
                recorder.close()
            if not preserve_workspace and workspace.exists():
                _remove_private_dir(workspace)
