import json

import pytest

from argus.adapters.base import AdapterError, PolicyAdapter
from argus.ates import AtesEventStore, EventType, RunId, to_json_compatible
from argus.engine.ates_runtime import (
    ActionOutcomeUnresolvedError,
    AtesAdapterProxy,
    AtesRuntimeRecorder,
)
from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from tests.conftest import FakeAdapter, FakeProvider


def _action(**values):
    return json.dumps(values)


def _events(project_dir, run_id):
    with AtesEventStore(project_dir, RunId(run_id)) as store:
        return tuple(store.events)


def _types(events):
    return [event.envelope.event_type for event in events]


def _payload(event):
    return to_json_compatible(event.payload)


def _one_step_spec():
    return parse_spec(
        """\
name: durable dispatch test
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "Type private-value"
"""
    )


def test_prepare_action_has_no_side_effect_and_dispatch_uses_normalized_action():
    class TrackingAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.events = []

        def validate_action(self, action):
            self.events.append(("validated", dict(action)))

        def act(self, action):
            self.events.append(("dispatched", dict(action)))
            return super().act(action)

    adapter = TrackingAdapter()
    prepared = adapter.prepare_action(
        {
            "action": " TYPE ",
            "text": "hello",
            "element_id": "1",
            "why": "model metadata is preserved outside the evidence boundary",
        }
    )

    assert prepared["action"] == "type"
    assert prepared["element_id"] == 1
    assert [kind for kind, _ in adapter.events] == ["validated"]
    assert adapter.app.text_content == ""

    note = adapter.dispatch_prepared_action(prepared)
    assert note == "typed 'hello'"
    assert [kind for kind, _ in adapter.events] == ["validated", "dispatched"]
    assert adapter.app.text_content == "hello"


def test_policy_adapter_keeps_legacy_act_compatibility_with_split_boundary():
    class TrackingAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.validated = 0
            self.dispatched = 0

        def validate_action(self, action):
            self.validated += 1

        def act(self, action):
            self.dispatched += 1
            return super().act(action)

    inner = TrackingAdapter()
    guarded = PolicyAdapter(inner)
    guarded.act({"action": "type", "text": "hello", "element_id": 1})

    assert inner.validated == 1
    assert inner.dispatched == 1
    assert inner.app.text_content == "hello"


def test_runtime_orders_validation_commit_and_terminal_outcome_with_one_operation_id(tmp_path):
    secret = "customer-secret-value"
    spec = parse_spec(
        f"""\
name: ordered dispatch evidence
target: {{adapter: desktop-gui, launch: fake.exe}}
steps:
  - "Type {secret}"
"""
    )
    provider = FakeProvider(
        [
            _action(action="type", text=secret, element_id=1, why="private rationale"),
            _action(action="done", success=True, note="finished"),
        ]
    )

    result = run_test(spec, provider, FakeAdapter(), project_dir=tmp_path)
    assert result.status == "pass"

    events = _events(tmp_path, result.ates_run_id)
    types = _types(events)
    action_types = [
        EventType.ACTION_PROPOSED,
        EventType.ACTION_POLICY_VALIDATED,
        EventType.ACTION_DISPATCH_COMMITTED,
        EventType.ACTION_EXECUTED,
    ]
    positions = [types.index(kind) for kind in action_types]
    assert positions == sorted(positions)

    proposed = _payload(events[positions[0]])["action"]
    validated = _payload(events[positions[1]])["action"]
    committed = _payload(events[positions[2]])["action"]
    executed = _payload(events[positions[3]])

    assert proposed["action_id"] == validated["action_id"] == committed["action_id"]
    assert (
        proposed["operation_id"]
        == validated["operation_id"]
        == committed["operation_id"]
        == executed["operation_id"]
    )
    assert committed["action_type"] == "type"
    assert committed["parameters"]["text"]["disposition"] == "redacted"

    canonical = b"".join(event.canonical_line() for event in events)
    assert secret.encode() not in canonical
    assert b"private rationale" not in canonical


def test_policy_rejection_never_crosses_dispatch_commit_boundary(tmp_path):
    class RejectingAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.dispatched = 0

        def validate_action(self, action):
            raise AdapterError("platform policy rejected action")

        def act(self, action):
            self.dispatched += 1
            return super().act(action)

    adapter = RejectingAdapter()
    provider = FakeProvider(
        [
            _action(action="type", text="private-value", element_id=1),
            _action(action="done", success=True, note="recovered"),
        ]
    )

    result = run_test(_one_step_spec(), provider, adapter, project_dir=tmp_path)
    events = _events(tmp_path, result.ates_run_id)
    types = _types(events)

    assert adapter.dispatched == 0
    assert EventType.ACTION_PROPOSED in types
    assert EventType.ACTION_POLICY_VALIDATED not in types
    assert EventType.ACTION_DISPATCH_COMMITTED not in types
    assert EventType.ACTION_EXECUTED not in types
    assert EventType.ACTION_OUTCOME_UNKNOWN not in types


def test_dispatch_failure_is_committed_then_unknown_and_blocks_blind_retry(tmp_path):
    class AmbiguousAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.dispatches = 0

        def act(self, action):
            self.dispatches += 1
            raise AdapterError("transport failed after dispatch may have reached target")

    adapter = AmbiguousAdapter()
    provider = FakeProvider(
        [
            _action(action="type", text="private-value", element_id=1),
            _action(action="type", text="must-not-retry", element_id=1),
            _action(action="done", success=True, note="must not mask ambiguity"),
        ]
    )

    result = run_test(_one_step_spec(), provider, adapter, project_dir=tmp_path)
    assert result.status == "error"
    assert adapter.dispatches == 1

    events = _events(tmp_path, result.ates_run_id)
    types = _types(events)
    assert types.count(EventType.ACTION_PROPOSED) == 1
    assert types.count(EventType.ACTION_POLICY_VALIDATED) == 1
    assert types.count(EventType.ACTION_DISPATCH_COMMITTED) == 1
    assert types.count(EventType.ACTION_OUTCOME_UNKNOWN) == 1
    assert EventType.ACTION_EXECUTED not in types

    committed = next(
        _payload(event)["action"]
        for event in events
        if event.envelope.event_type is EventType.ACTION_DISPATCH_COMMITTED
    )
    unknown = next(
        _payload(event)
        for event in events
        if event.envelope.event_type is EventType.ACTION_OUTCOME_UNKNOWN
    )
    assert committed["operation_id"] == unknown["operation_id"]

    canonical = b"".join(event.canonical_line() for event in events)
    assert b"private-value" not in canonical
    assert b"must-not-retry" not in canonical
    assert b"transport failed after dispatch" not in canonical


def test_dispatch_commit_failure_prevents_target_side_effect(tmp_path, monkeypatch):
    spec = _one_step_spec()
    provider = FakeProvider([])

    class CountingAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.dispatches = 0

        def act(self, action):
            self.dispatches += 1
            return super().act(action)

    adapter = CountingAdapter()
    recorder = AtesRuntimeRecorder.for_scripted(tmp_path, spec, provider, adapter)
    recorder.begin_step(0)
    proxy = AtesAdapterProxy(adapter, recorder)

    def fail_commit(*_args, **_kwargs):
        raise OSError("simulated durable commit failure")

    monkeypatch.setattr(recorder, "record_action_dispatch_committed", fail_commit)

    with pytest.raises(OSError, match="durable commit failure"):
        proxy.act({"action": "type", "text": "never-dispatched", "element_id": 1})

    assert adapter.dispatches == 0
    assert adapter.app.text_content == ""
    recorder.complete_current("error")
    recorder.close()


def test_missing_terminal_evidence_blocks_future_target_interaction(tmp_path, monkeypatch):
    spec = _one_step_spec()
    provider = FakeProvider([])

    class CountingAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.dispatches = 0

        def act(self, action):
            self.dispatches += 1
            return super().act(action)

    adapter = CountingAdapter()
    recorder = AtesRuntimeRecorder.for_scripted(tmp_path, spec, provider, adapter)
    recorder.begin_step(0)
    proxy = AtesAdapterProxy(adapter, recorder)

    def fail_terminal(_action_record):
        raise OSError("simulated terminal evidence failure")

    monkeypatch.setattr(recorder, "record_action_executed", fail_terminal)

    with pytest.raises(OSError, match="terminal evidence failure"):
        proxy.act({"action": "type", "text": "side-effect-happened", "element_id": 1})

    assert adapter.dispatches == 1
    assert adapter.app.text_content == "side-effect-happened"
    assert proxy.unresolved_operation_id is not None

    with pytest.raises(ActionOutcomeUnresolvedError, match="refusing further target interaction"):
        proxy.observe(include_screenshot=False)
    with pytest.raises(ActionOutcomeUnresolvedError, match="refusing further target interaction"):
        proxy.act({"action": "wait", "seconds": 0.1})

    recorder.complete_current("outcome_unknown")
    recorder.close()
