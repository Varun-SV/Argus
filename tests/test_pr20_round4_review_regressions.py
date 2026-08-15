import json

import pytest

from argus.ates import (
    AtesEventStore,
    EvidenceContext,
    EvidencePrivacyConfig,
    EvidencePrivacyPolicy,
    EventType,
    RunId,
    to_json_compatible,
)
from argus.engine.ates_runtime import AtesRuntimeRecorder
from argus.engine.spec import parse_spec
from tests.conftest import FakeAdapter, FakeProvider


class _RecordingSink:
    def __init__(self):
        self.calls = []
        self.refs = []

    def put(self, value, *, context, field_name, protected_ref):
        self.calls.append((value, context, field_name))
        self.refs.append(protected_ref)


def _spec():
    return parse_spec(
        """\
name: privacy isolation regression
target: {adapter: desktop-gui, launch: fake.exe}
retries: 1
steps:
  - "Complete the operation"
"""
    )


def _events(project_dir, run_id):
    with AtesEventStore(project_dir, RunId(str(run_id))) as store:
        return tuple(store.events)


def _scheduled_reason(project_dir, run_id):
    event = next(
        event
        for event in _events(project_dir, run_id)
        if event.envelope.event_type is EventType.STEP_RETRY_SCHEDULED
    )
    return to_json_compatible(event.payload)["reason"]


def test_run_snapshots_policy_config_and_sink_before_provenance_commit(tmp_path):
    first_sink = _RecordingSink()
    second_sink = _RecordingSink()
    caller_policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            policy_id="snapshot-policy-v1",
            protected_contexts=frozenset({EvidenceContext.ACTION_PARAMETER}),
        ),
        protected_sink=first_sink,
    )
    committed_policy_id = caller_policy.policy_id

    with pytest.raises(AttributeError):
        caller_policy.config = EvidencePrivacyConfig()  # type: ignore[misc]
    with pytest.raises(AttributeError):
        caller_policy.protected_sink = second_sink  # type: ignore[misc]

    recorder = AtesRuntimeRecorder.for_scripted(
        tmp_path,
        _spec(),
        FakeProvider([]),
        FakeAdapter(),
        privacy_policy=caller_policy,
    )
    try:
        assert recorder.privacy is not caller_policy
        assert recorder.run_record.evidence_profile == committed_policy_id
        assert recorder.privacy.policy_id == committed_policy_id

        recorder.begin_step(0, 1)

        # Even a caller deliberately bypassing the read-only public properties
        # cannot change the run-local snapshot after provenance was committed.
        caller_policy._config = EvidencePrivacyConfig(  # type: ignore[attr-defined]
            policy_id="mutated-policy",
            protected_contexts=frozenset(),
        )
        caller_policy._protected_sink = second_sink  # type: ignore[attr-defined]

        proposed = recorder.record_action_proposed(
            {"action": "click", "element_id": 7}
        )
        validated = recorder.record_action_policy_validated(
            proposed,
            {"action": "click", "element_id": 7},
        )
        recorder.complete_current("pass")

        projected = to_json_compatible(validated)["parameters"]["element_id"]
        assert projected["disposition"] == "protected_ref"
        assert projected["protected_ref"].startswith("protected://ates/")
        assert recorder.privacy.policy_id == committed_policy_id
        assert recorder.run_record.evidence_profile == committed_policy_id
        assert first_sink.calls
        assert second_sink.calls == []
    finally:
        recorder.close()


def test_failed_retry_append_cannot_poison_later_run_using_same_caller_policy(
    tmp_path,
    monkeypatch,
):
    sink = _RecordingSink()
    shared_policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset({EvidenceContext.RETRY_REASON})
        ),
        protected_sink=sink,
    )

    first = AtesRuntimeRecorder.for_scripted(
        tmp_path,
        _spec(),
        FakeProvider([]),
        FakeAdapter(),
        privacy_policy=shared_policy,
    )
    first_attempt = first.begin_step(0, 1)
    assert first.complete_current("fail") == first_attempt
    first.privacy.prepare_retry_reason("run A private retry cause")

    original_append = first._store.append

    def fail_retry_append(event_type, payload):
        if event_type is EventType.STEP_RETRY_SCHEDULED:
            raise OSError("scheduled retry persistence failed")
        return original_append(event_type, payload)

    monkeypatch.setattr(first._store, "append", fail_retry_append)
    with pytest.raises(OSError, match="scheduled retry persistence failed"):
        first.schedule_retry(0, first_attempt, 2)
    assert first.failed is True
    first.close()

    second = AtesRuntimeRecorder.for_scripted(
        tmp_path,
        _spec(),
        FakeProvider([]),
        FakeAdapter(),
        privacy_policy=shared_policy,
    )
    second_attempt = second.begin_step(0, 1)
    assert second.complete_current("fail") == second_attempt
    second.privacy.prepare_retry_reason("run B private retry cause")
    next_id = second.schedule_retry(0, second_attempt, 2)
    second.begin_step(0, 2, attempt_id=next_id, retry=True)
    second.complete_current("pass")
    second_run_id = second.run_id
    second.close()

    assert [call[0] for call in sink.calls] == [
        "run A private retry cause",
        "run B private retry cause",
    ]
    second_reason = _scheduled_reason(tmp_path, second_run_id)
    assert second_reason["protected_ref"] == sink.refs[1]
    assert second_reason["protected_ref"] != sink.refs[0]


def test_interleaved_runs_do_not_overwrite_each_others_retry_reason(tmp_path):
    sink = _RecordingSink()
    shared_policy = EvidencePrivacyPolicy(
        EvidencePrivacyConfig(
            protected_contexts=frozenset({EvidenceContext.RETRY_REASON})
        ),
        protected_sink=sink,
    )
    left = AtesRuntimeRecorder.for_scripted(
        tmp_path,
        _spec(),
        FakeProvider([]),
        FakeAdapter(),
        privacy_policy=shared_policy,
    )
    right = AtesRuntimeRecorder.for_scripted(
        tmp_path,
        _spec(),
        FakeProvider([]),
        FakeAdapter(),
        privacy_policy=shared_policy,
    )

    left_attempt = left.begin_step(0, 1)
    left.complete_current("fail")
    right_attempt = right.begin_step(0, 1)
    right.complete_current("fail")

    # Interleave preparation before either scheduled event is emitted. With
    # one shared mutable policy, the second call would overwrite the first.
    left.privacy.prepare_retry_reason("left private retry cause")
    right.privacy.prepare_retry_reason("right private retry cause")

    left_next = left.schedule_retry(0, left_attempt, 2)
    right_next = right.schedule_retry(0, right_attempt, 2)
    left.begin_step(0, 2, attempt_id=left_next, retry=True)
    right.begin_step(0, 2, attempt_id=right_next, retry=True)
    left.complete_current("pass")
    right.complete_current("pass")
    left_run_id = left.run_id
    right_run_id = right.run_id
    left.close()
    right.close()

    assert [call[0] for call in sink.calls] == [
        "left private retry cause",
        "right private retry cause",
    ]
    left_reason = _scheduled_reason(tmp_path, left_run_id)
    right_reason = _scheduled_reason(tmp_path, right_run_id)
    assert left_reason["protected_ref"] == sink.refs[0]
    assert right_reason["protected_ref"] == sink.refs[1]
    assert left_reason["protected_ref"] != right_reason["protected_ref"]
