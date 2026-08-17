from __future__ import annotations

import hashlib
import hmac

import pytest

from argus.ates import (
    ArtifactCaptureConfig,
    ArtifactCaptureError,
    ArtifactCapturePolicy,
    ArtifactContext,
    AtesArtifactRepository,
    AtesEventStore,
    EvidenceDisposition,
    RunId,
)


def _repo(tmp_path, policy=None):
    store = AtesEventStore(tmp_path, RunId.new())
    return store, AtesArtifactRepository(store, policy)


def _artifact_files(store):
    root = store.run_dir / "artifacts"
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def _expected_protected_commitment(store, payload):
    digest = hashlib.sha256(payload).digest()
    key = (store.run_dir / ".ates-artifact-hmac-key").read_bytes()
    return "hmac:" + hmac.new(key, digest, hashlib.sha256).hexdigest()


def test_standard_screenshot_is_protected_and_uses_secret_safe_commitment(tmp_path):
    raw = b"\x89PNG\r\nprivate-screen-bytes"
    raw_digest = hashlib.sha256(raw).hexdigest()
    store, repository = _repo(tmp_path)
    try:
        result = repository.capture_bytes(
            raw,
            context=ArtifactContext.FAILURE_SCREENSHOT,
            kind="screenshot",
            media_type="image/png",
        )
        assert result.suppression is None
        record = result.record
        assert record is not None
        assert record.protection_state is EvidenceDisposition.PROTECTED_REF
        assert record.protected_ref.startswith("protected://ates/")
        assert record.path.startswith("artifacts/protected/screenshot/ART-")
        assert record.path.endswith(".png")
        assert record.size_bytes == len(raw)
        assert record.content_digest.method == "hmac-sha256"
        assert record.content_digest.value == _expected_protected_commitment(store, raw)
        assert raw_digest not in record.content_digest.value
        assert record.content_digest.verification_ref == "secret://ates/run-artifact-hmac-key"
        retained = store.run_dir / record.path
        assert retained.read_bytes() == raw
        assert retained.stat().st_nlink == 1
    finally:
        store.close()


def test_suppressed_artifact_never_creates_a_payload_file(tmp_path):
    policy = ArtifactCapturePolicy(
        ArtifactCaptureConfig(
            suppressed_contexts=frozenset({ArtifactContext.FAILURE_SCREENSHOT})
        )
    )
    store, repository = _repo(tmp_path, policy)
    try:
        result = repository.capture_bytes(
            b"secret screenshot",
            context=ArtifactContext.FAILURE_SCREENSHOT,
            kind="screenshot",
            media_type="image/png",
        )
        assert result.record is None
        assert result.suppression is not None
        assert result.suppression.reason == "artifact.policy_suppressed"
        assert _artifact_files(store) == []
        assert not (store.run_dir / ".ates-artifact-hmac-key").exists()
    finally:
        store.close()


def test_redaction_happens_in_memory_before_first_payload_write(tmp_path):
    raw = b"raw-secret-image"
    masked = b"masked-image"

    class Masker:
        calls = []

        def sanitize(self, data, *, context, media_type):
            self.calls.append((data, context, media_type))
            assert _artifact_files(store) == []
            return masked

    masker = Masker()
    policy = ArtifactCapturePolicy(
        ArtifactCaptureConfig(
            redacted_contexts=frozenset({ArtifactContext.FAILURE_SCREENSHOT})
        ),
        sanitizer=masker,
    )
    store, repository = _repo(tmp_path, policy)
    try:
        result = repository.capture_bytes(
            raw,
            context=ArtifactContext.FAILURE_SCREENSHOT,
            kind="screenshot",
            media_type="image/png",
        )
        record = result.record
        assert record is not None
        assert record.protection_state is EvidenceDisposition.REDACTED
        files = _artifact_files(store)
        assert len(files) == 1
        assert files[0].read_bytes() == masked
        assert raw not in files[0].read_bytes()
        assert record.size_bytes == len(masked)
        assert record.content_digest.value == "sha256:" + hashlib.sha256(masked).hexdigest()
        assert record.content_digest.verification_ref is None
        assert not (store.run_dir / ".ates-artifact-hmac-key").exists()
        assert masker.calls == [(raw, ArtifactContext.FAILURE_SCREENSHOT, "image/png")]
    finally:
        store.close()


def test_redacted_capture_fails_closed_without_sanitizer(tmp_path):
    policy = ArtifactCapturePolicy(
        ArtifactCaptureConfig(
            redacted_contexts=frozenset({ArtifactContext.FAILURE_SCREENSHOT})
        )
    )
    store, repository = _repo(tmp_path, policy)
    try:
        with pytest.raises(ArtifactCaptureError, match="requires an in-memory sanitizer"):
            repository.capture_bytes(
                b"must-not-hit-disk",
                context=ArtifactContext.FAILURE_SCREENSHOT,
                kind="screenshot",
                media_type="image/png",
            )
        assert _artifact_files(store) == []
    finally:
        store.close()


def test_mutable_input_is_snapshotted_before_sanitizer_or_persistence(tmp_path):
    original = bytearray(b"original")

    class MutatingSanitizer:
        def sanitize(self, data, *, context, media_type):
            original[:] = b"mutated!"
            assert data == b"original"
            return data + b"-masked"

    policy = ArtifactCapturePolicy(
        ArtifactCaptureConfig(
            redacted_contexts=frozenset({ArtifactContext.FAILURE_SCREENSHOT})
        ),
        sanitizer=MutatingSanitizer(),
    )
    store, repository = _repo(tmp_path, policy)
    try:
        result = repository.capture_bytes(
            original,
            context=ArtifactContext.FAILURE_SCREENSHOT,
            kind="screenshot",
            media_type="image/png",
        )
        record = result.record
        assert record is not None
        assert (store.run_dir / record.path).read_bytes() == b"original-masked"
        assert original == bytearray(b"mutated!")
    finally:
        store.close()


def test_oversized_artifact_is_suppressed_before_tree_creation(tmp_path):
    policy = ArtifactCapturePolicy(ArtifactCaptureConfig(max_artifact_bytes=4))
    store, repository = _repo(tmp_path, policy)
    try:
        result = repository.capture_bytes(
            b"12345",
            context=ArtifactContext.FINDING_SCREENSHOT,
            kind="screenshot",
            media_type="image/png",
        )
        assert result.record is None
        assert result.suppression is not None
        assert result.suppression.reason == "artifact.too_large"
        assert _artifact_files(store) == []
    finally:
        store.close()


def test_artifact_tree_rejects_traversal_and_noncanonical_paths(tmp_path):
    store, repository = _repo(tmp_path)
    try:
        for value in ("../secret.png", "/absolute.png", "x/../../secret.png", "x\\evil.png"):
            with pytest.raises((ArtifactCaptureError, ValueError)):
                with repository.open_tree((value,)):
                    pass
    finally:
        store.close()


def test_protected_collection_reservations_are_opaque_and_content_independent(tmp_path):
    store, repository = _repo(tmp_path)
    try:
        first, second = repository.reserve_protected_collection(2)
        assert first.relative_path != second.relative_path
        assert first.relative_path.startswith("protected/collected_file/ART-")
        assert second.relative_path.startswith("protected/collected_file/ART-")
        assert "customer" not in first.relative_path
        assert first.protected_ref.startswith("protected://ates/")
        assert second.protected_ref.startswith("protected://ates/")
    finally:
        store.close()


def test_finalize_reserved_binds_exact_persisted_bytes_without_raw_hash_disclosure(tmp_path):
    payload = b"guest-generated-private-file"
    raw_digest = hashlib.sha256(payload).hexdigest()
    store, repository = _repo(tmp_path)
    try:
        reservation = repository.reserve_protected_collection(1)[0]
        with repository.open_tree((reservation.relative_path,)) as tree:
            handle, temp_name, _ = tree.open_temp_file(reservation.relative_path)
            with handle:
                handle.write(payload)
                handle.flush()
            tree.commit_temp(reservation.relative_path, temp_name)
            record = repository.finalize_reserved(
                tree,
                reservation,
                expected_size=len(payload),
                expected_sha256=raw_digest,
            )
        assert record.protection_state is EvidenceDisposition.PROTECTED_REF
        assert record.path == reservation.artifact_path
        assert record.content_digest.method == "hmac-sha256"
        assert record.content_digest.value == _expected_protected_commitment(store, payload)
        assert raw_digest not in record.content_digest.value
        assert (store.run_dir / record.path).read_bytes() == payload
    finally:
        store.close()


def test_finalize_reserved_rejects_size_or_digest_drift(tmp_path):
    payload = b"payload"
    store, repository = _repo(tmp_path)
    try:
        reservations = repository.reserve_protected_collection(2)
        with repository.open_tree(tuple(item.relative_path for item in reservations)) as tree:
            for reservation in reservations:
                handle, temp_name, _ = tree.open_temp_file(reservation.relative_path)
                with handle:
                    handle.write(payload)
                    handle.flush()
                tree.commit_temp(reservation.relative_path, temp_name)

            with pytest.raises(ArtifactCaptureError, match="size changed"):
                repository.finalize_reserved(
                    tree,
                    reservations[0],
                    expected_size=len(payload) + 1,
                )
            with pytest.raises(ArtifactCaptureError, match="digest changed"):
                repository.finalize_reserved(
                    tree,
                    reservations[1],
                    expected_sha256="0" * 64,
                )
    finally:
        store.close()


def test_policy_context_sets_cannot_overlap():
    with pytest.raises(ValueError, match="must not overlap"):
        ArtifactCaptureConfig(
            safe_contexts=frozenset({ArtifactContext.CHECKPOINT_SCREENSHOT}),
            suppressed_contexts=frozenset({ArtifactContext.CHECKPOINT_SCREENSHOT}),
        )
