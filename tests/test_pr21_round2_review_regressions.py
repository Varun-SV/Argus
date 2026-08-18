from __future__ import annotations

import hashlib
import json
import os

import pytest

import argus.ates.artifacts as artifacts_module
from argus.ates import (
    ArtifactCaptureConfig,
    ArtifactCaptureError,
    ArtifactCapturePolicy,
    ArtifactContext,
    ArtifactId,
    ArtifactSuppression,
    AtesArtifactRepository,
    AtesEventStore,
    EventType,
    RunId,
    to_json_compatible,
)
from argus.engine.ates_runtime import AtesRuntimeError
from argus.engine.roam import roam
from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from argus.execution.ates_collection import collect_capsule_artifacts_to_tree
from argus.tokens import Budget
from tests.conftest import FakeAdapter, FakeProvider


def _action(**values):
    return json.dumps(values)


def _events(project_dir, run_id):
    with AtesEventStore(project_dir, RunId(str(run_id))) as store:
        return tuple(store.events)


def _runs(project_dir):
    root = project_dir / ".argus" / "runs"
    return set(root.glob("RUN-*")) if root.exists() else set()


class _FixedSanitizer:
    def sanitize(self, data, *, context, media_type):
        assert data == b"raw-private-payload"
        assert context is ArtifactContext.FAILURE_SCREENSHOT
        assert media_type == "image/png"
        return b"sanitized-payload"


def test_redacted_artifact_rejects_post_policy_byte_replacement(tmp_path, monkeypatch):
    store = AtesEventStore(tmp_path, RunId.new())
    policy = ArtifactCapturePolicy(
        ArtifactCaptureConfig(
            redacted_contexts=frozenset({ArtifactContext.FAILURE_SCREENSHOT})
        ),
        sanitizer=_FixedSanitizer(),
    )
    repository = AtesArtifactRepository(store, policy)
    original_commit = artifacts_module._AtesArtifactTree.commit_temp

    def corrupt_after_commit(self, relative, temp_name):
        original_commit(self, relative, temp_name)
        self.lexical_path(relative).write_bytes(b"replacement-private-bytes")

    monkeypatch.setattr(
        artifacts_module._AtesArtifactTree,
        "commit_temp",
        corrupt_after_commit,
    )
    try:
        with pytest.raises(ArtifactCaptureError, match="policy-approved snapshot"):
            repository.capture_bytes(
                b"raw-private-payload",
                context=ArtifactContext.FAILURE_SCREENSHOT,
                kind="screenshot",
                media_type="image/png",
            )
    finally:
        store.close()

    artifact_root = next((tmp_path / ".argus" / "runs").glob("RUN-*")) / "artifacts"
    retained_files = [path for path in artifact_root.rglob("*") if path.is_file()]
    assert retained_files == []


def test_suppression_reason_rejects_regex_valid_secret_like_identifier():
    with pytest.raises(ValueError, match="supported safe reason code"):
        ArtifactSuppression(
            artifact_id=ArtifactId.new(),
            context=ArtifactContext.FAILURE_SCREENSHOT,
            kind="screenshot",
            capture_policy="ates-artifact-v1",
            reason="customer-password-was-visible",
        )


def test_rejected_scripted_artifact_policy_creates_no_started_run(tmp_path):
    spec = parse_spec(
        """\
name: rejected scripted artifact policy
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "finish"
"""
    )
    nonstandard = ArtifactCapturePolicy(
        ArtifactCaptureConfig(
            safe_contexts=frozenset({ArtifactContext.FAILURE_SCREENSHOT})
        )
    )
    before = _runs(tmp_path)

    result = run_test(
        spec,
        FakeProvider([_action(action="done", success=True)]),
        FakeAdapter(),
        project_dir=tmp_path,
        artifact_policy=nonstandard,
    )

    assert result.status == "error"
    assert "non-standard runtime artifact policy" in (result.error or "")
    assert not result.ates_run_id
    assert _runs(tmp_path) == before


def test_rejected_roam_artifact_policy_creates_no_started_run(tmp_path):
    nonstandard = ArtifactCapturePolicy(
        ArtifactCaptureConfig(
            safe_contexts=frozenset({ArtifactContext.FINDING_SCREENSHOT})
        )
    )
    before = _runs(tmp_path)

    with pytest.raises(AtesRuntimeError, match="non-standard runtime artifact policy"):
        roam(
            target="fake.exe",
            provider=FakeProvider([]),
            adapter=FakeAdapter(),
            budget=Budget(max_tokens=1000),
            session_dir=tmp_path / ".argus" / "roam" / "rejected-round2",
            project_dir=tmp_path,
            artifact_policy=nonstandard,
            generate_regressions=False,
        )

    assert _runs(tmp_path) == before


def test_text_only_roam_captures_finding_evidence_without_sending_pixels_to_model(
    tmp_path, monkeypatch
):
    import argus.engine.roam_impl as roam_impl

    monkeypatch.setattr(roam_impl.time, "sleep", lambda _seconds: None)
    provider = FakeProvider(
        [
            _action(
                action="report_bug",
                title="text-only finding",
                severity="medium",
                expected="expected",
                actual="actual",
                why="detail",
            )
        ],
        vision=False,
    )
    session = roam(
        target="fake.exe",
        provider=provider,
        adapter=FakeAdapter(),
        budget=Budget(max_tokens=121, tracker=provider.tracker),
        session_dir=tmp_path / ".argus" / "roam" / "text-only-evidence",
        project_dir=tmp_path,
        generate_regressions=False,
    )

    assert len(session.findings) == 1
    assert provider.calls[0]["images"] is None
    events = _events(tmp_path, session.ates_run_id)
    finding = next(
        to_json_compatible(event.payload)["finding"]
        for event in events
        if event.envelope.event_type is EventType.FINDING_RECORDED
    )
    checkpoints = [
        to_json_compatible(event.payload)
        for event in events
        if event.envelope.event_type is EventType.CHECKPOINT_CAPTURED
        and to_json_compatible(event.payload)["context"] == "finding_screenshot"
    ]
    suppressions = [
        to_json_compatible(event.payload)
        for event in events
        if event.envelope.event_type is EventType.ARTIFACT_SUPPRESSED
        and to_json_compatible(event.payload)["context"] == "finding_screenshot"
    ]
    assert len(checkpoints) == 1
    assert suppressions == []
    assert checkpoints[0]["finding_id"] == finding["finding_id"]
    artifact = checkpoints[0]["artifact"]
    retained = tmp_path / ".argus" / "runs" / session.ates_run_id / artifact["path"]
    assert retained.read_bytes() == b"\x89PNG fake"


class _MissingInfoClient:
    def collect_info(self, path):
        raise ValueError(f"missing declared artifact: {path}")

    def _request(self, method, endpoint):  # pragma: no cover
        raise AssertionError("no stream request expected")


class _OversizeInfoClient:
    def collect_info(self, path):
        return {
            "size": artifacts_module._DEFAULT_MAX_ARTIFACT_BYTES + 1,
            "sha256": hashlib.sha256(b"not-transferred").hexdigest(),
        }

    def _request(self, method, endpoint):  # pragma: no cover
        raise AssertionError("no stream request expected")


class _CollectingAdapter(FakeAdapter):
    environment_type = "capsule"

    def __init__(self, client):
        super().__init__()
        self._workspace_ready = True
        self._client = client

    def prepare_transfers(self):
        self._workspace_ready = True

    def collect_artifacts_to_tree(self, entries, output_tree):
        return collect_capsule_artifacts_to_tree(self, entries, output_tree)


def _collection_failure_result(tmp_path, filename, client):
    spec = parse_spec(
        f"""\
name: collection suppression review
target: {{adapter: desktop-gui, launch: fake.exe}}
steps:
  - "finish"
collect:
  - {filename}
"""
    )
    return run_test(
        spec,
        FakeProvider([_action(action="done", success=True)]),
        _CollectingAdapter(client),
        project_dir=tmp_path,
    )


@pytest.mark.parametrize(
    ("client", "expected_reason"),
    [
        (_MissingInfoClient(), "artifact.capture_unavailable"),
        (_OversizeInfoClient(), "artifact.too_large"),
    ],
)
def test_collection_preflight_failure_emits_secret_safe_suppression(
    tmp_path, client, expected_reason
):
    secret_name = "customer-passwords.txt"
    result = _collection_failure_result(tmp_path, secret_name, client)

    assert result.status == "error"
    assert secret_name not in (result.transfer_error or "")
    assert secret_name not in (result.error or "")
    events = _events(tmp_path, result.ates_run_id)
    canonical = b"".join(event.canonical_line() for event in events)
    assert secret_name.encode() not in canonical
    suppressions = [
        to_json_compatible(event.payload)
        for event in events
        if event.envelope.event_type is EventType.ARTIFACT_SUPPRESSED
        and to_json_compatible(event.payload)["context"] == "collected_file"
    ]
    assert len(suppressions) == 1
    assert suppressions[0]["reason"] == expected_reason
    assert suppressions[0]["collection_ordinal"] == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX hardlink publication regression")
def test_partial_posix_publication_uses_durable_rollback_barrier(tmp_path, monkeypatch):
    store = AtesEventStore(tmp_path, RunId.new())
    repository = AtesArtifactRepository(store)
    real_unlink = artifacts_module.os.unlink
    failed_temp_unlink = False

    def fail_first_temp_unlink(path, *args, **kwargs):
        nonlocal failed_temp_unlink
        name = str(path)
        if not failed_temp_unlink and ".argus-" in name and name.endswith(".part"):
            failed_temp_unlink = True
            raise OSError("simulated temp unlink failure after hardlink publication")
        return real_unlink(path, *args, **kwargs)

    real_fsync = artifacts_module._PinnedDirectory.fsync
    fsync_calls = []

    def track_fsync(self):
        fsync_calls.append(self.path)
        return real_fsync(self)

    real_rollback = artifacts_module._AtesArtifactTree._rollback_published
    rollback_barriers = []

    def track_rollback(parent, final_name):
        before = len(fsync_calls)
        result = real_rollback(parent, final_name)
        rollback_barriers.append(len(fsync_calls) > before)
        return result

    monkeypatch.setattr(artifacts_module.os, "unlink", fail_first_temp_unlink)
    monkeypatch.setattr(artifacts_module._PinnedDirectory, "fsync", track_fsync)
    monkeypatch.setattr(
        artifacts_module._AtesArtifactTree,
        "_rollback_published",
        staticmethod(track_rollback),
    )
    try:
        with pytest.raises(ArtifactCaptureError, match="artifact commit failed"):
            repository.capture_bytes(
                b"private artifact bytes",
                context=ArtifactContext.FAILURE_SCREENSHOT,
                kind="screenshot",
                media_type="image/png",
            )
    finally:
        store.close()

    assert failed_temp_unlink is True
    assert rollback_barriers and all(rollback_barriers)
    artifact_root = next((tmp_path / ".argus" / "runs").glob("RUN-*")) / "artifacts"
    assert [path for path in artifact_root.rglob("*") if path.is_file()] == []
