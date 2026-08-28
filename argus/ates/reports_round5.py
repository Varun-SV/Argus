"""Round-5 point-in-time external report binding for PR #22.

A trusted report-manifest digest authenticates a historical derived bundle.  A
later detached approval/audit append may make that bundle stale for
REGENERATED_VERIFIED, but it must not invalidate the exact externally bound
bytes or their canonical-evidence source binding.
"""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping, Sequence
from pathlib import Path

from . import reports_runtime as _runtime
from .finalization import FinalizationError, FinalizationTrustState


def _verify_bound_snapshot(
    run_dir,
    trusted_report_manifest_digest: str,
):
    root = _runtime._root(run_dir)
    _runtime.verify_finalized_run(root)
    report_dir = root / "reports"
    manifest_raw = _runtime._pinned_bytes(
        report_dir,
        "report-manifest-0001.json",
        "report manifest",
    )

    if (
        not isinstance(trusted_report_manifest_digest, str)
        or not trusted_report_manifest_digest.startswith("sha256:")
        or len(trusted_report_manifest_digest) != len("sha256:") + 64
    ):
        raise _runtime.ReportError(
            "trusted report-manifest digest must be sha256:<64 lowercase/uppercase hex>"
        )
    expected = trusted_report_manifest_digest[len("sha256:") :]
    try:
        int(expected, 16)
    except ValueError as exc:
        raise _runtime.ReportError(
            "trusted report-manifest digest must contain hexadecimal SHA-256"
        ) from exc
    actual_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
    if not hmac.compare_digest(actual_digest.lower(), trusted_report_manifest_digest.lower()):
        raise _runtime.ReportError("external report-manifest binding does not verify")

    manifest = _runtime._strict_object(manifest_raw, "report manifest")
    if manifest.get("report_manifest_version") != _runtime.REPORT_MANIFEST_VERSION:
        raise _runtime.ReportError("unsupported report manifest version")
    renderer = manifest.get("renderer")
    if not isinstance(renderer, Mapping) or renderer.get("id") != _runtime.REPORT_RENDERER_ID:
        raise _runtime.ReportError("unsupported report renderer identity")

    source = manifest.get("source_evidence_manifest")
    source_raw = _runtime._pinned_bytes(
        root / "manifests",
        "manifest-0001.json",
        "source evidence manifest",
    )
    source_digest = "sha256:" + hashlib.sha256(source_raw).hexdigest()
    if (
        not isinstance(source, Mapping)
        or source.get("path") != "manifests/manifest-0001.json"
        or not isinstance(source.get("sha256"), str)
        or not hmac.compare_digest(source_digest, source["sha256"])
    ):
        raise _runtime.ReportError("report manifest source binding does not verify")

    listed = manifest.get("members")
    if (
        isinstance(listed, (str, bytes, bytearray, Mapping))
        or not isinstance(listed, Sequence)
    ):
        raise _runtime.ReportError("report manifest members are malformed")
    by_path: dict[str, Mapping[str, object]] = {}
    for item in tuple(listed):
        if (
            not isinstance(item, Mapping)
            or not isinstance(item.get("path"), str)
            or item["path"] in by_path
        ):
            raise _runtime.ReportError(
                "report manifest contains malformed/duplicate member"
            )
        by_path[item["path"]] = item

    for name in _runtime._REPORT_NAMES:
        meta = by_path.get("reports/" + name)
        if meta is None:
            raise _runtime.ReportError(f"report manifest is missing {name}")
        data = _runtime._pinned_bytes(report_dir, name, f"rendered report {name}")
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        if (
            meta.get("size_bytes") != len(data)
            or not isinstance(meta.get("sha256"), str)
            or not hmac.compare_digest(digest, meta["sha256"])
        ):
            raise _runtime.ReportError(f"report byte binding failed for {name}")

    return _runtime.ReportVerificationResult(
        FinalizationTrustState.BOUND_VERIFIED,
        report_dir,
        report_dir / "report-manifest-0001.json",
    )


def install() -> None:
    previous_verify = _runtime.verify_report_bundle

    def verify_report_bundle(
        run_dir: Path | str,
        *,
        trusted_report_manifest_digest=None,
        approval_key_resolver=None,
    ):
        if trusted_report_manifest_digest is None:
            return previous_verify(
                run_dir,
                approval_key_resolver=approval_key_resolver,
            )
        try:
            return _verify_bound_snapshot(run_dir, trusted_report_manifest_digest)
        except (RuntimeError, _runtime.ReportError, FinalizationError, OSError, ValueError) as exc:
            try:
                report_dir = _runtime._root(run_dir) / "reports"
            except _runtime.ReportError:
                report_dir = Path(run_dir) / "reports"
            return _runtime.ReportVerificationResult(
                FinalizationTrustState.INVALID,
                report_dir,
                report_dir / "report-manifest-0001.json",
                str(exc),
            )

    _runtime.verify_report_bundle = verify_report_bundle


__all__ = ["install"]
