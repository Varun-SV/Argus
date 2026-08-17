from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
from pathlib import Path

import pytest

from argus.ates import AtesEventStore, EventType, RunId, to_json_compatible
from argus.engine.ates_artifacts import RuntimeArtifactCapture
from argus.engine.ates_runtime import AtesRuntimeRecorder
from argus.engine.roam import roam
from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from argus.execution.ates_collection import collect_capsule_artifacts_to_tree
from argus.tokens import Budget
from tests.conftest import FakeAdapter, FakeProvider


def _action(**values):
    return json.dumps(values)


def _events(project_dir, run_id):
    with AtesEventStore(project_dir, RunId(run_id)) as store:
        return tuple(store.events)


def _artifact_files(project_dir, run_id):
    root = project_dir / ".argus" / "runs" / run_id / "artifacts"
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def test_failing_assertion_captures_exact_observation_as_protected_artifact(tmp_path):
    spec = parse_spec(
        """\
name: artifact assertion failure
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - assert:
      text_visible: "not-present"
"""
    )
    legacy_shots = tmp_path / "legacy-shots"
    result = run_test(
        spec,
        FakeProvider([]),
        FakeAdapter(),
        shots_dir=legacy_shots,
        project_dir=tmp_path,
    )
    assert result.status == "fail"
    assert not legacy_shots.exists()

    events = _events(tmp_path, result.ates_run_id)
    checkpoints = [
        event for event in events
        if event.envelope.event_type is EventType.CHECKPOINT_CAPTURED
    ]
    assert len(checkpoints) == 1
    payload = to_json_compatible(checkpoints[0].payload)
    assert payload["context"] == "failure_screenshot"
    artifact = payload["artifact"]
    assert artifact["protection_state"] == "protected_ref"
    assert artifact["path"].startswith("artifacts/protected/screenshot/ART-")
    assert artifact["protected_ref"].startswith("protected://ates/")
    retained = tmp_path / ".argus" / "runs" / result.ates_run_id / artifact["path"]
    assert retained.read_bytes() == b"\x89PNG fake"


def test_successful_nl_step_does_not_create_automatic_ates_screenshot(tmp_path):
    spec = parse_spec(
        """\
name: no pass screenshot
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "Finish successfully"
"""
    )
    result = run_test(
        spec,
        FakeProvider([_action(action="done", success=True)]),
        FakeAdapter(),
        shots_dir=tmp_path / "legacy-shots",
        project_dir=tmp_path,
    )
    assert result.status == "pass"
    events = _events(tmp_path, result.ates_run_id)
    assert EventType.CHECKPOINT_CAPTURED not in [
        event.envelope.event_type for event in events
    ]
    assert EventType.ARTIFACT_SUPPRESSED not in [
        event.envelope.event_type for event in events
    ]
    assert _artifact_files(tmp_path, result.ates_run_id) == []


def test_failing_nl_step_uses_protected_failure_capture_not_legacy_shots(tmp_path):
    spec = parse_spec(
        """\
name: nl failure screenshot
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "Operation must fail"
"""
    )
    legacy_shots = tmp_path / "legacy-shots"
    result = run_test(
        spec,
        FakeProvider([_action(action="done", success=False, note="failed")]),
        FakeAdapter(),
        shots_dir=legacy_shots,
        project_dir=tmp_path,
    )
    assert result.status == "fail"
    assert not legacy_shots.exists()
    events = _events(tmp_path, result.ates_run_id)
    assert sum(
        event.envelope.event_type is EventType.CHECKPOINT_CAPTURED
        for event in events
    ) == 1
    assert len(_artifact_files(tmp_path, result.ates_run_id)) == 1


def test_roam_finding_screenshot_never_reaches_legacy_shots(tmp_path, monkeypatch):
    import argus.engine.roam_impl as roam_impl

    monkeypatch.setattr(roam_impl.time, "sleep", lambda _seconds: None)
    provider = FakeProvider(
        [
            _action(
                action="report_bug",
                title="private finding",
                severity="medium",
                expected="expected",
                actual="actual",
            )
        ]
    )
    session_dir = tmp_path / ".argus" / "roam" / "protected-session"
    session = roam(
        target="private.exe",
        provider=provider,
        adapter=FakeAdapter(),
        budget=Budget(max_tokens=241, tracker=provider.tracker),
        session_dir=session_dir,
        project_dir=tmp_path,
        generate_regressions=False,
    )
    assert len(session.findings) == 1
    assert session.findings[0].screenshot is None
    shots = session_dir / "shots"
    assert not shots.exists() or list(shots.glob("*.png")) == []

    events = _events(tmp_path, session.ates_run_id)
    finding_checkpoint = next(
        to_json_compatible(event.payload)
        for event in events
        if event.envelope.event_type is EventType.CHECKPOINT_CAPTURED
        and to_json_compatible(event.payload)["context"] == "finding_screenshot"
    )
    artifact = finding_checkpoint["artifact"]
    retained = tmp_path / ".argus" / "runs" / session.ates_run_id / artifact["path"]
    assert retained.read_bytes() == b"\x89PNG fake"


class _StreamingClient:
    def __init__(self, payloads, *, fail_path=None):
        self.payloads = dict(payloads)
        self.fail_path = fail_path

    def collect_info(self, path):
        payload = self.payloads[path]
        return {
            "size": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }

    def _request(self, method, endpoint):
        assert method == "GET"
        parsed = urllib.parse.urlparse(endpoint)
        query = urllib.parse.parse_qs(parsed.query)
        path = query["path"][0]
        if path == self.fail_path:
            raise RuntimeError("simulated private guest transfer failure")
        offset = int(query["offset"][0])
        limit = int(query["limit"][0])
        chunk = self.payloads[path][offset : offset + limit]
        return {"data_b64": base64.b64encode(chunk).decode("ascii")}


class _MappedCapsule(FakeAdapter):
    environment_type = "capsule"

    def __init__(self, client):
        super().__init__()
        self._workspace_ready = True
        self._client = client

    def collect_artifacts_to_tree(self, entries, output_tree):
        return collect_capsule_artifacts_to_tree(self, entries, output_tree)


def _collection_recorder(tmp_path):
    spec = parse_spec(
        """\
name: protected collection
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "done"
"""
    )
    recorder = AtesRuntimeRecorder.for_scripted(
        tmp_path,
        spec,
        FakeProvider([]),
        FakeAdapter(),
    )
    return recorder, RuntimeArtifactCapture(recorder)


def test_mapped_collection_hides_guest_filename_from_canonical_evidence(tmp_path):
    secret_name = "customer-passwords.txt"
    payload = b"sensitive collected bytes"
    recorder, artifacts = _collection_recorder(tmp_path)
    run_id = str(recorder.run_id)
    try:
        result = artifacts.collect_declared(
            _MappedCapsule(_StreamingClient({secret_name: payload})),
            [secret_name],
        )
        assert len(result) == 1
        assert secret_name not in repr(result)
        assert result[0]["protected"] is True
    finally:
        recorder.close()

    events = _events(tmp_path, run_id)
    canonical = b"".join(event.canonical_line() for event in events)
    assert secret_name.encode() not in canonical
    assert payload not in canonical
    collected = next(
        to_json_compatible(event.payload)["artifact"]
        for event in events
        if event.envelope.event_type is EventType.ARTIFACT_COLLECTED
    )
    assert collected["path"].startswith("artifacts/protected/collected_file/ART-")
    assert secret_name not in collected["path"]
    retained = tmp_path / ".argus" / "runs" / run_id / collected["path"]
    assert retained.read_bytes() == payload


def test_mapped_collection_rolls_back_prior_files_and_emits_no_events_on_failure(tmp_path):
    first = "first-private.txt"
    second = "second-private.txt"
    recorder, artifacts = _collection_recorder(tmp_path)
    run_id = str(recorder.run_id)
    client = _StreamingClient(
        {first: b"first", second: b"second"},
        fail_path=second,
    )
    try:
        with pytest.raises(Exception, match="protected Capsule artifact collection failed"):
            artifacts.collect_declared(_MappedCapsule(client), [first, second])
    finally:
        recorder.close()

    events = _events(tmp_path, run_id)
    assert EventType.ARTIFACT_COLLECTED not in [
        event.envelope.event_type for event in events
    ]
    assert _artifact_files(tmp_path, run_id) == []
