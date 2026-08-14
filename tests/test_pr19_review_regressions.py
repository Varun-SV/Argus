import json

import pytest

from argus.adapters.base import AdapterError, PolicyAdapter
from argus.ates import AtesEventStore, EventType, RunId, to_json_compatible
from argus.capsule.guest_agent import GuestAgentState
from argus.engine.ates_runtime import AtesAdapterProxy, AtesRuntimeRecorder
from argus.engine.roam import roam
from argus.engine.spec import parse_spec
from argus.execution.capsule import CapsuleExecutionEnvironment
from argus.tokens import Budget
from tests.conftest import FakeAdapter, FakeProvider


def _events(project_dir, run_id):
    with AtesEventStore(project_dir, RunId(run_id)) as store:
        return tuple(store.events)


def test_guest_prepares_platform_action_before_one_shot_dispatch():
    class TrackingPlatformAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.validated = 0
            self.dispatched = 0

        def validate_action(self, action):
            self.validated += 1

        def act(self, action):
            self.dispatched += 1
            return super().act(action)

    platform = TrackingPlatformAdapter()
    state = GuestAgentState()
    state.adapter = PolicyAdapter(platform)

    prepared = state.prepare_action(
        {"action": " TYPE ", "text": "hello", "element_id": "1"}
    )

    assert platform.validated == 1
    assert platform.dispatched == 0
    assert prepared["action"]["action"] == "type"
    assert prepared["action"]["element_id"] == 1
    token = prepared["prepared_token"]

    note = state.dispatch_prepared_action(token)
    assert note == "typed 'hello'"
    assert platform.dispatched == 1
    assert platform.app.text_content == "hello"

    with pytest.raises(AdapterError, match="unknown or already consumed"):
        state.dispatch_prepared_action(token)
    assert platform.dispatched == 1


def test_guest_platform_rejection_never_produces_prepared_operation():
    class RejectingPlatformAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.dispatched = 0

        def validate_action(self, action):
            raise AdapterError("guest platform policy rejected action")

        def act(self, action):
            self.dispatched += 1
            return super().act(action)

    platform = RejectingPlatformAdapter()
    state = GuestAgentState()
    state.adapter = PolicyAdapter(platform)

    with pytest.raises(AdapterError, match="guest platform policy rejected"):
        state.prepare_action(
            {"action": "type", "text": "must-not-dispatch", "element_id": 1}
        )

    assert platform.dispatched == 0
    assert state._prepared_actions == {}


def test_capsule_ates_commit_sits_between_guest_prepare_and_dispatch(tmp_path, monkeypatch):
    calls = []

    class FakeControlClient:
        def _request(self, method, path, payload=None):
            assert method == "POST"
            if path == "/v1/action/prepare":
                calls.append("guest_prepare")
                action = dict(payload["action"])
                action["action"] = str(action["action"]).strip().lower()
                action["element_id"] = int(action["element_id"])
                return {
                    "ok": True,
                    "prepared_token": "prepared-token-1",
                    "action": action,
                }
            if path == "/v1/action/dispatch":
                calls.append("guest_dispatch")
                assert payload == {"prepared_token": "prepared-token-1"}
                return {"ok": True, "note": "guest dispatched"}
            raise AssertionError(path)

    environment = object.__new__(CapsuleExecutionEnvironment)
    environment.type_name = "desktop-gui"
    environment._client = FakeControlClient()
    environment._adapter = object()

    spec = parse_spec(
        """\
name: capsule prepared dispatch order
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "Type a value"
"""
    )
    provider = FakeProvider([])
    recorder = AtesRuntimeRecorder.for_scripted(
        tmp_path,
        spec,
        provider,
        FakeAdapter(),
    )
    recorder.begin_step(0)
    proxy = AtesAdapterProxy(environment, recorder)

    original_commit = recorder.record_action_dispatch_committed

    def record_commit(*args, **kwargs):
        calls.append("ates_commit")
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(recorder, "record_action_dispatch_committed", record_commit)

    note = proxy.act(
        {"action": " TYPE ", "text": "private-value", "element_id": "1"}
    )

    assert note == "guest dispatched"
    assert calls == ["guest_prepare", "ates_commit", "guest_dispatch"]

    events = _events(tmp_path, str(recorder.run_id))
    types = [event.envelope.event_type for event in events]
    assert EventType.ACTION_POLICY_VALIDATED in types
    assert EventType.ACTION_DISPATCH_COMMITTED in types
    assert EventType.ACTION_EXECUTED in types

    recorder.complete_current("pass")
    recorder.close()


def test_capsule_guest_prepare_rejection_never_crosses_ates_commit(tmp_path):
    class RejectingControlClient:
        def _request(self, method, path, payload=None):
            assert method == "POST"
            assert path == "/v1/action/prepare"
            raise AdapterError("guest platform rejected before dispatch")

    environment = object.__new__(CapsuleExecutionEnvironment)
    environment.type_name = "desktop-gui"
    environment._client = RejectingControlClient()
    environment._adapter = object()

    spec = parse_spec(
        """\
name: capsule deterministic rejection
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "Type a value"
"""
    )
    provider = FakeProvider([])
    recorder = AtesRuntimeRecorder.for_scripted(
        tmp_path,
        spec,
        provider,
        FakeAdapter(),
    )
    recorder.begin_step(0)
    proxy = AtesAdapterProxy(environment, recorder)

    with pytest.raises(AdapterError, match="guest platform rejected before dispatch"):
        proxy.act({"action": "type", "text": "private-value", "element_id": 1})

    events = _events(tmp_path, str(recorder.run_id))
    types = [event.envelope.event_type for event in events]
    assert EventType.ACTION_PROPOSED in types
    assert EventType.ACTION_POLICY_VALIDATED not in types
    assert EventType.ACTION_DISPATCH_COMMITTED not in types
    assert EventType.ACTION_OUTCOME_UNKNOWN not in types

    recorder.complete_current("error")
    recorder.close()


def test_roam_unresolved_dispatch_stops_cleanly_and_writes_outputs(tmp_path, monkeypatch):
    import argus.engine.roam_impl as roam_impl

    monkeypatch.setattr(roam_impl.time, "sleep", lambda _seconds: None)

    class AmbiguousAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.dispatches = 0

        def act(self, action):
            self.dispatches += 1
            raise AdapterError("transport failed after dispatch may have reached target")

    adapter = AmbiguousAdapter()
    provider = FakeProvider(
        [json.dumps({"action": "click", "element_id": 2})]
    )
    budget = Budget(max_tokens=241, tracker=provider.tracker)
    session_dir = tmp_path / ".argus" / "roam" / "unresolved"

    session = roam(
        target="private-target.exe",
        provider=provider,
        adapter=adapter,
        budget=budget,
        session_dir=session_dir,
        project_dir=tmp_path,
        generate_regressions=False,
    )

    assert adapter.dispatches == 1
    assert "action outcome unresolved after durable dispatch commit" in session.stopped_reason
    assert (session_dir / "report.md").is_file()
    assert (session_dir / "session.json").is_file()

    session_json = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    assert "action outcome unresolved" in session_json["stopped_reason"]
    report = (session_dir / "report.md").read_text(encoding="utf-8")
    assert "action outcome unresolved" in report

    events = _events(tmp_path, session.ates_run_id)
    types = [event.envelope.event_type for event in events]
    assert types.count(EventType.ACTION_PROPOSED) == 1
    assert types.count(EventType.ACTION_POLICY_VALIDATED) == 1
    assert types.count(EventType.ACTION_DISPATCH_COMMITTED) == 1
    assert types.count(EventType.ACTION_OUTCOME_UNKNOWN) == 1

    completion = next(
        to_json_compatible(event.payload)["attempt"]
        for event in events
        if event.envelope.event_type is EventType.STEP_ATTEMPT_COMPLETED
    )
    assert completion["status"] == "outcome_unknown"

    incomplete = next(
        to_json_compatible(event.payload)
        for event in events
        if event.envelope.event_type is EventType.RUN_MARKED_INCOMPLETE
    )
    assert incomplete["execution_result"] == "outcome_unknown"
