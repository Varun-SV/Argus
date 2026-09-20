import json

from argus.ates import AtesEventStore, EventType, RunId, to_json_compatible
from argus.engine.ates_runtime import AtesRuntimeRecorder
from argus.engine.roam import roam
from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from argus.tokens import Budget
from tests.conftest import FakeAdapter, FakeProvider


def _action(**values):
    return json.dumps(values)


def _events(project_dir, run_id):
    with AtesEventStore(project_dir, RunId(run_id)) as store:
        return tuple(store.events)


def _canonical_bytes(events):
    return b"".join(event.canonical_line() for event in events)


def test_scripted_run_emits_structural_ates_without_sensitive_values(tmp_path):
    secret = "s3cr3t-runtime-value"
    target = "private-target.exe"
    spec = parse_spec(
        f"""\
name: structural ATES test
target:
  adapter: desktop-gui
  launch: {target}
steps:
  - "Type {secret} into the editor"
  - assert:
      text_visible: "{secret}"
teardown:
  - close
"""
    )
    provider = FakeProvider(
        [
            _action(action="type", text=secret, element_id=1, why="enter private value"),
            _action(action="done", success=True, note="private value entered"),
        ]
    )

    result = run_test(
        spec,
        provider,
        FakeAdapter(),
        project_dir=tmp_path,
    )

    assert result.status == "pass"
    assert result.ates_run_id.startswith("RUN-")

    events = _events(tmp_path, result.ates_run_id)
    types = [event.envelope.event_type for event in events]
    assert types[0] is EventType.RUN_STARTED
    assert EventType.ENVIRONMENT_PREPARED in types
    assert EventType.TARGET_LAUNCHED in types
    assert EventType.STEP_ATTEMPT_STARTED in types
    assert EventType.OBSERVATION_CAPTURED in types
    assert EventType.ACTION_PROPOSED in types
    assert EventType.ACTION_EXECUTED in types
    assert EventType.ASSERTION_EVALUATED in types
    assert EventType.STEP_ATTEMPT_COMPLETED in types
    assert EventType.TARGET_CLOSED in types
    assert EventType.ENVIRONMENT_RELEASED in types
    assert EventType.RUN_MARKED_INCOMPLETE in types
    assert types[-1] is EventType.RUN_COMPLETED
    assert all(event.sequence == index for index, event in enumerate(events, 1))

    persisted = _canonical_bytes(events)
    assert secret.encode() not in persisted
    assert target.encode() not in persisted
    assert b"private value entered" not in persisted
    assert b"enter private value" not in persisted

    proposed = next(
        event for event in events if event.envelope.event_type is EventType.ACTION_PROPOSED
    )
    action = to_json_compatible(proposed.payload)["action"]
    assert action["action_type"] == "type"
    assert action["parameters"]["text"]["disposition"] == "redacted"
    assert action["parameters"]["element_id"]["disposition"] == "redacted"

    completion = next(
        event for event in reversed(events)
        if event.envelope.event_type is EventType.RUN_MARKED_INCOMPLETE
    )
    assert to_json_compatible(completion.payload)["execution_result"] == "pass"


def test_scripted_retries_get_distinct_attempt_identity_and_ordinals(tmp_path):
    spec = parse_spec(
        """\
name: retry ATES test
target: {adapter: desktop-gui, launch: fake.exe}
retries: 1
steps:
  - "Complete the operation"
"""
    )
    provider = FakeProvider(
        [
            _action(action="done", success=False, note="first attempt failed"),
            _action(action="done", success=True, note="second attempt passed"),
        ]
    )

    result = run_test(spec, provider, FakeAdapter(), project_dir=tmp_path)
    assert result.status == "pass"
    assert result.steps[0].flaky is True

    events = _events(tmp_path, result.ates_run_id)
    starts = [
        to_json_compatible(event.payload)["attempt"]
        for event in events
        if event.envelope.event_type is EventType.STEP_ATTEMPT_STARTED
    ]
    completions = [
        to_json_compatible(event.payload)["attempt"]
        for event in events
        if event.envelope.event_type is EventType.STEP_ATTEMPT_COMPLETED
    ]
    retries = [
        event for event in events
        if event.envelope.event_type is EventType.STEP_RETRY_SCHEDULED
    ]

    assert [item["attempt"] for item in starts] == [1, 2]
    assert starts[0]["step_attempt_id"] != starts[1]["step_attempt_id"]
    assert starts[0]["step_id"] == starts[1]["step_id"]
    assert [item["status"] for item in completions] == ["failed", "passed"]
    assert len(retries) == 1
    retry = to_json_compatible(retries[0].payload)
    assert retry["previous_step_attempt_id"] == starts[0]["step_attempt_id"]
    assert retry["next_step_attempt_id"] == starts[1]["step_attempt_id"]
    assert retry["next_attempt"] == 2


def test_failed_action_is_recorded_as_unknown_without_action_payload(tmp_path):
    from argus.adapters.base import AdapterError

    class RejectingAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.dispatches = 0

        def act(self, action):
            self.dispatches += 1
            raise AdapterError("blocked private-secret-detail")

    spec = parse_spec(
        """\
name: failed action ATES test
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "Type ultra-private-input"
"""
    )
    provider = FakeProvider(
        [
            _action(action="type", text="ultra-private-input", element_id=1),
            _action(action="type", text="must-not-blindly-retry", element_id=1),
            _action(action="done", success=True),
        ]
    )

    # Once a validated action has a durable dispatch commit, a dispatch failure
    # is ambiguous: the target may already have observed the side effect. PR #19
    # therefore stops rather than allowing a later model turn to mask or retry it.
    adapter = RejectingAdapter()
    result = run_test(spec, provider, adapter, project_dir=tmp_path)
    assert result.status == "error"
    assert adapter.dispatches == 1

    events = _events(tmp_path, result.ates_run_id)
    types = [event.envelope.event_type for event in events]
    assert types.count(EventType.ACTION_PROPOSED) == 1
    assert EventType.ACTION_POLICY_VALIDATED in types
    assert EventType.ACTION_DISPATCH_COMMITTED in types
    assert EventType.ACTION_OUTCOME_UNKNOWN in types
    assert EventType.ACTION_EXECUTED not in types
    persisted = _canonical_bytes(events)
    assert b"ultra-private-input" not in persisted
    assert b"must-not-blindly-retry" not in persisted
    assert b"blocked private-secret-detail" not in persisted


def test_assertion_values_are_suppressed_but_result_is_canonical(tmp_path):
    spec = parse_spec(
        """\
name: assertion privacy test
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - assert:
      text_visible: "customer-private-token"
"""
    )
    result = run_test(spec, FakeProvider([]), FakeAdapter(), project_dir=tmp_path)
    assert result.status == "fail"

    events = _events(tmp_path, result.ates_run_id)
    assertion_event = next(
        event for event in events
        if event.envelope.event_type is EventType.ASSERTION_EVALUATED
    )
    assertion = to_json_compatible(assertion_event.payload)["assertion"]
    assert assertion["kind"] == "text_visible"
    assert assertion["result"] == "failed"
    assert assertion["expected"]["disposition"] == "redacted"
    assert assertion["actual"]["disposition"] == "suppressed"
    assert b"customer-private-token" not in _canonical_bytes(events)


def test_roam_emits_structural_observations_actions_and_findings(tmp_path, monkeypatch):
    import argus.engine.roam_impl as roam_impl

    monkeypatch.setattr(roam_impl.time, "sleep", lambda _seconds: None)
    secret_target = "private-roam-target.exe"
    secret_finding = "customer password appeared in dialog"
    provider = FakeProvider(
        [
            _action(action="click", element_id=2, why="private navigation detail"),
            _action(
                action="report_bug",
                title=secret_finding,
                severity="medium",
                expected="private expected text",
                actual="private actual text",
                why="private analysis text",
            ),
        ]
    )
    budget = Budget(max_tokens=241, tracker=provider.tracker)
    session_dir = tmp_path / ".argus" / "roam" / "session"

    session = roam(
        target=secret_target,
        provider=provider,
        adapter=FakeAdapter(),
        budget=budget,
        session_dir=session_dir,
        project_dir=tmp_path,
        generate_regressions=False,
    )

    assert session.ates_run_id.startswith("RUN-")
    assert len(session.findings) == 1

    events = _events(tmp_path, session.ates_run_id)
    types = [event.envelope.event_type for event in events]
    assert EventType.OBSERVATION_CAPTURED in types
    assert EventType.ACTION_PROPOSED in types
    assert EventType.ACTION_EXECUTED in types
    assert EventType.FINDING_RECORDED in types
    assert EventType.STEP_ATTEMPT_COMPLETED in types
    assert EventType.RUN_MARKED_INCOMPLETE in types
    assert types[-1] is EventType.RUN_COMPLETED

    finding_event = next(
        event for event in events if event.envelope.event_type is EventType.FINDING_RECORDED
    )
    finding = to_json_compatible(finding_event.payload)["finding"]
    assert finding["classification"] == "medium"
    assert finding["classification_source"] == "model"
    assert finding["title"]["disposition"] == "suppressed"
    assert finding["description"]["disposition"] == "suppressed"

    persisted = _canonical_bytes(events)
    for value in (
        secret_target,
        secret_finding,
        "private navigation detail",
        "private expected text",
        "private actual text",
        "private analysis text",
    ):
        assert value.encode() not in persisted


def test_action_proposal_allowlists_action_and_parameter_names(tmp_path):
    secret_key_name = "customer-secret-parameter-name"
    secret_action_name = "customer-secret-action"
    spec = parse_spec(
        """\
name: action structure safety
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "Exercise action validation"
"""
    )
    provider = FakeProvider(
        [
            json.dumps(
                {
                    "action": "type",
                    "text": "private-value",
                    "element_id": 1,
                    secret_key_name: "private-extra-value",
                }
            ),
            json.dumps({"action": secret_action_name, secret_key_name: "private-value"}),
            _action(action="done", success=True),
        ]
    )

    result = run_test(spec, provider, FakeAdapter(), project_dir=tmp_path)
    events = _events(tmp_path, result.ates_run_id)
    proposed = [
        to_json_compatible(event.payload)["action"]
        for event in events
        if event.envelope.event_type is EventType.ACTION_PROPOSED
    ]

    assert proposed[0]["action_type"] == "type"
    assert set(proposed[0]["parameters"]) == {"text", "element_id"}
    assert any(item["action_type"] == "invalid" for item in proposed)
    persisted = _canonical_bytes(events)
    assert secret_key_name.encode() not in persisted
    assert secret_action_name.encode() not in persisted
    assert b"private-extra-value" not in persisted


def test_model_finding_severity_is_allowlisted_before_persistence(tmp_path):
    secret_severity = "customer-private-severity-text"
    recorder = AtesRuntimeRecorder.for_roam(
        tmp_path,
        FakeProvider([]),
        FakeAdapter(),
        target="fake.exe",
    )
    run_id = str(recorder.run_id)
    recorder.record_finding(source="model", classification=secret_severity)
    recorder.close()

    events = _events(tmp_path, run_id)
    finding_event = next(
        event for event in events if event.envelope.event_type is EventType.FINDING_RECORDED
    )
    finding = to_json_compatible(finding_event.payload)["finding"]
    assert finding["classification_source"] == "model"
    assert finding["classification"] == "unclassified"
    assert secret_severity.encode() not in _canonical_bytes(events)


def test_scripted_source_identity_is_stable_and_content_distinguishing(tmp_path):
    provider = FakeProvider([])
    first = parse_spec(
        """\
name: logical test alpha
target: {adapter: desktop-gui, launch: private-a.exe}
steps:
  - "Type secret-alpha"
"""
    )
    changed = parse_spec(
        """\
name: logical test alpha
target: {adapter: desktop-gui, launch: private-a.exe}
steps:
  - "Type secret-beta"
"""
    )
    unrelated = parse_spec(
        """\
name: logical test beta
target: {adapter: desktop-gui, launch: private-a.exe}
steps:
  - "Type secret-alpha"
"""
    )

    rec_first = AtesRuntimeRecorder.for_scripted(tmp_path, first, provider, FakeAdapter())
    rec_changed = AtesRuntimeRecorder.for_scripted(tmp_path, changed, provider, FakeAdapter())
    rec_unrelated = AtesRuntimeRecorder.for_scripted(tmp_path, unrelated, provider, FakeAdapter())
    try:
        assert rec_first.run_record.source.test_case_id == rec_changed.run_record.source.test_case_id
        assert rec_first.run_record.source.test_case_id != rec_unrelated.run_record.source.test_case_id
        assert (
            rec_first.run_record.source.commitment.value
            != rec_changed.run_record.source.commitment.value
        )
        assert rec_first.run_record.source.commitment.method == "hmac-sha256"
        assert rec_first.run_record.source.commitment.verification_ref
    finally:
        rec_first.close()
        rec_changed.close()
        rec_unrelated.close()

    persisted = b"".join(
        _canonical_bytes(_events(tmp_path, str(recorder.run_id)))
        for recorder in (rec_first, rec_changed, rec_unrelated)
    )
    for secret in ("secret-alpha", "secret-beta", "private-a.exe"):
        assert secret.encode() not in persisted


def test_roam_source_has_no_fake_objective_and_binds_target_secret_safely(tmp_path):
    provider = FakeProvider([])
    first = AtesRuntimeRecorder.for_roam(
        tmp_path,
        provider,
        FakeAdapter(),
        target="private-roam-a.exe",
    )
    second = AtesRuntimeRecorder.for_roam(
        tmp_path,
        provider,
        FakeAdapter(),
        target="private-roam-b.exe",
    )
    try:
        first_source = first.run_record.source
        second_source = second.run_record.source
        assert first_source.objective_present is False
        assert first_source.objective_commitment is None
        assert first_source.config_commitment is not None
        assert second_source.config_commitment is not None
        assert first_source.config_commitment.value != second_source.config_commitment.value
    finally:
        first.close()
        second.close()

    persisted = _canonical_bytes(_events(tmp_path, str(first.run_id))) + _canonical_bytes(
        _events(tmp_path, str(second.run_id))
    )
    assert b"private-roam-a.exe" not in persisted
    assert b"private-roam-b.exe" not in persisted


def test_model_identity_is_present_secret_safe_and_changes_configuration(tmp_path):
    spec = parse_spec(
        """\
name: model provenance test
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "Observe model identity"
"""
    )
    first_provider = FakeProvider([])
    first_provider.model = "private-model-alpha"
    second_provider = FakeProvider([])
    second_provider.model = "private-model-beta"

    first = AtesRuntimeRecorder.for_scripted(tmp_path, spec, first_provider, FakeAdapter())
    second = AtesRuntimeRecorder.for_scripted(tmp_path, spec, second_provider, FakeAdapter())
    try:
        assert first.run_record.model_provider == "fake"
        assert first.run_record.model.startswith("MODEL-")
        assert first.run_record.model != second.run_record.model
        assert (
            first.run_record.configuration_commitment.value
            != second.run_record.configuration_commitment.value
        )
    finally:
        first.close()
        second.close()

    persisted = _canonical_bytes(_events(tmp_path, str(first.run_id))) + _canonical_bytes(
        _events(tmp_path, str(second.run_id))
    )
    assert b"private-model-alpha" not in persisted
    assert b"private-model-beta" not in persisted
