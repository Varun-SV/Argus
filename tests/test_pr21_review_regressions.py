from __future__ import annotations

import json

import pytest

from argus.ates import (
    ArtifactCaptureConfig,
    ArtifactCapturePolicy,
    ArtifactContext,
    AtesEventStore,
    EventType,
    RunId,
    to_json_compatible,
)
from argus.engine.ates_runtime import AtesRuntimeError, AtesRuntimeRecorder
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


class _RejectingInfoClient:
    def collect_info(self, path):
        raise ValueError(f"guest artifact size/checksum is invalid for {path}")

    def _request(self, method, endpoint):  # pragma: no cover - preflight must fail first
        raise AssertionError("stream request must not happen after rejected preflight")


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


def test_collection_preflight_guest_name_never_reaches_legacy_or_canonical_metadata(tmp_path):
    secret_name = "customer-passwords.txt"
    spec = parse_spec(
        f"""\
name: collection preflight privacy
target: {{adapter: desktop-gui, launch: fake.exe}}
steps:
  - "finish"
collect:
  - {secret_name}
"""
    )
    result = run_test(
        spec,
        FakeProvider([_action(action="done", success=True)]),
        _CollectingAdapter(_RejectingInfoClient()),
        project_dir=tmp_path,
    )

    assert result.transfer_error
    assert "protected Capsule artifact collection preflight failed" in result.transfer_error
    assert secret_name not in result.transfer_error
    assert result.error is not None
    assert secret_name not in result.error

    events = _events(tmp_path, result.ates_run_id)
    canonical = b"".join(event.canonical_line() for event in events)
    assert secret_name.encode() not in canonical


def test_public_roam_missing_finding_screenshot_emits_linked_suppression(tmp_path, monkeypatch):
    import argus.engine.roam_impl as roam_impl

    monkeypatch.setattr(roam_impl.time, "sleep", lambda _seconds: None)
    adapter = FakeAdapter()
    adapter.app.alive = False
    provider = FakeProvider([])
    session = roam(
        target="crashing.exe",
        provider=provider,
        adapter=adapter,
        budget=Budget(max_tokens=1000, tracker=provider.tracker),
        session_dir=tmp_path / ".argus" / "roam" / "missing-shot",
        project_dir=tmp_path,
        generate_regressions=False,
    )

    events = _events(tmp_path, session.ates_run_id)
    finding = next(
        to_json_compatible(event.payload)["finding"]
        for event in events
        if event.envelope.event_type is EventType.FINDING_RECORDED
    )
    suppression = next(
        to_json_compatible(event.payload)
        for event in events
        if event.envelope.event_type is EventType.ARTIFACT_SUPPRESSED
        and to_json_compatible(event.payload)["context"] == "finding_screenshot"
    )
    assert suppression["reason"] == "artifact.screenshot_unavailable"
    assert suppression["finding_id"] == finding["finding_id"]
    assert suppression["artifact_id"].startswith("ART-")


def test_rejected_roam_artifact_policy_closes_new_recorder(tmp_path, monkeypatch):
    import argus.engine.ates_runtime as ates_runtime

    closed_run_ids = []
    original_close = ates_runtime.AtesRuntimeRecorder.close

    def tracking_close(self):
        closed_run_ids.append(str(self.run_id))
        return original_close(self)

    monkeypatch.setattr(ates_runtime.AtesRuntimeRecorder, "close", tracking_close)
    nonstandard = ArtifactCapturePolicy(
        ArtifactCaptureConfig(
            safe_contexts=frozenset({ArtifactContext.FINDING_SCREENSHOT})
        )
    )
    provider = FakeProvider([])

    with pytest.raises(AtesRuntimeError, match="non-standard runtime artifact policy"):
        roam(
            target="fake.exe",
            provider=provider,
            adapter=FakeAdapter(),
            budget=Budget(max_tokens=1000, tracker=provider.tracker),
            session_dir=tmp_path / ".argus" / "roam" / "rejected-policy",
            project_dir=tmp_path,
            artifact_policy=nonstandard,
            generate_regressions=False,
        )

    assert len(closed_run_ids) == 1
    # Reopening the same run namespace proves the writer lock/handle was released.
    _events(tmp_path, closed_run_ids[0])


def test_two_roam_findings_each_have_their_own_screenshot_relationship(tmp_path, monkeypatch):
    import argus.engine.roam_impl as roam_impl

    monkeypatch.setattr(roam_impl.time, "sleep", lambda _seconds: None)
    provider = FakeProvider(
        [
            json.dumps(
                [
                    {
                        "action": "report_bug",
                        "title": "first finding",
                        "severity": "medium",
                        "expected": "first expected",
                        "actual": "first actual",
                        "why": "first detail",
                    },
                    {
                        "action": "report_bug",
                        "title": "second finding",
                        "severity": "high",
                        "expected": "second expected",
                        "actual": "second actual",
                        "why": "second detail",
                    },
                ]
            )
        ]
    )
    session = roam(
        target="fake.exe",
        provider=provider,
        adapter=FakeAdapter(),
        budget=Budget(max_tokens=1000, tracker=provider.tracker),
        session_dir=tmp_path / ".argus" / "roam" / "two-findings",
        project_dir=tmp_path,
        stop_flag=lambda: len(provider.calls) >= 1,
        generate_regressions=False,
    )
    assert len(session.findings) == 2

    events = _events(tmp_path, session.ates_run_id)
    finding_ids = [
        to_json_compatible(event.payload)["finding"]["finding_id"]
        for event in events
        if event.envelope.event_type is EventType.FINDING_RECORDED
    ]
    checkpoints = [
        to_json_compatible(event.payload)
        for event in events
        if event.envelope.event_type is EventType.CHECKPOINT_CAPTURED
        and to_json_compatible(event.payload)["context"] == "finding_screenshot"
    ]

    assert len(finding_ids) == 2
    assert len(checkpoints) == 2
    assert len(set(finding_ids)) == 2
    assert {checkpoint["finding_id"] for checkpoint in checkpoints} == set(finding_ids)
    assert len({checkpoint["artifact"]["artifact_id"] for checkpoint in checkpoints}) == 2
