import json

from argus.ates import AtesEventStore, EventType, RunId, to_json_compatible
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


def _event_docs(events):
    return [event.to_document() for event in events]


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
    assert types[-1] is EventType.RUN_MARKED_INCOMPLETE
    assert EventType.RUN_COMPLETED not in types
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
    assert action["parameters"]["text"]["disposition"] == "suppressed"
    assert action["parameters"]["element_id"]["disposition"] == "suppressed"

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
        def act(self, action):
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
        [_action(action="type", text="ultra-private-input", element_id=1)]
    )

    result = run_test(spec, provider, RejectingAdapter(), project_dir=tmp_path)
    assert result.status == "error"

    events = _events(tmp_path, result.ates_run_id)
    types = [event.envelope.event_type for event in events]
    assert EventType.ACTION_PROPOSED in types
    assert EventType.ACTION_OUTCOME_UNKNOWN in types
    assert EventType.ACTION_EXECUTED not in types
    persisted = _canonical_bytes(events)
    assert b"ultra-private-input" not in persisted
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
    assert assertion["expected"]["disposition"] == "suppressed"
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
    assert types[-1] is EventType.RUN_MARKED_INCOMPLETE
    assert EventType.RUN_COMPLETED not in types

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
