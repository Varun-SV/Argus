import json

from click.testing import CliRunner

from argus.adapters.base import PolicyAdapter
from argus.ates import AtesEventStore, EventType, RunId
from argus.engine.ates_runtime import AtesAdapterProxy, AtesRuntimeRecorder
from argus.engine.roam import RoamSession, roam
from argus.engine.spec import parse_spec
from argus.execution.base import LocalExecutionEnvironment
from argus.tokens import Budget
from tests.conftest import FakeAdapter, FakeProvider


def _events(project_dir, run_id):
    with AtesEventStore(project_dir, RunId(run_id)) as store:
        return tuple(store.events)


def test_local_environment_validates_exactly_once_before_ates_commit(tmp_path):
    calls = []

    class TrackingAdapter(FakeAdapter):
        def validate_action(self, action):
            calls.append(("validated", dict(action)))

        def act(self, action):
            calls.append(("dispatched", dict(action)))
            return super().act(action)

    inner = TrackingAdapter()
    environment = LocalExecutionEnvironment(PolicyAdapter(inner))
    provider = FakeProvider([])
    spec = parse_spec(
        """\
name: local prepared dispatch order
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "Type a value"
"""
    )
    recorder = AtesRuntimeRecorder.for_scripted(
        tmp_path,
        spec,
        provider,
        environment,
    )
    recorder.begin_step(0)
    proxy = AtesAdapterProxy(environment, recorder)

    original_commit = recorder.record_action_dispatch_committed

    def record_commit(*args, **kwargs):
        calls.append(("ates_commit", None))
        return original_commit(*args, **kwargs)

    recorder.record_action_dispatch_committed = record_commit

    note = proxy.act(
        {"action": " TYPE ", "text": "private-value", "element_id": "1"}
    )
    assert note == "typed 'private-value'"
    assert [kind for kind, _ in calls] == ["validated", "ates_commit", "dispatched"]
    assert calls[0][1]["action"] == "type"
    assert calls[2][1] == calls[0][1]

    recorder.complete_current("pass")
    run_id = str(recorder.run_id)
    recorder.close()

    types = [event.envelope.event_type for event in _events(tmp_path, run_id)]
    assert EventType.ACTION_POLICY_VALIDATED in types
    assert EventType.ACTION_DISPATCH_COMMITTED in types
    assert EventType.ACTION_EXECUTED in types


def test_public_roam_finalizes_outputs_when_terminal_evidence_persistence_fails(
    tmp_path, monkeypatch
):
    import argus.engine.roam_impl as roam_impl

    monkeypatch.setattr(roam_impl.time, "sleep", lambda _seconds: None)
    seen = {}

    def fail_executed(self, _action):
        error = OSError("simulated ACTION_EXECUTED persistence failure")
        self._failed = True
        self._failure = error
        seen["recorder"] = self
        raise error

    monkeypatch.setattr(
        AtesRuntimeRecorder,
        "record_action_executed",
        fail_executed,
    )

    adapter = FakeAdapter()
    provider = FakeProvider(
        [json.dumps({"action": "click", "element_id": 2})]
    )
    budget = Budget(max_tokens=241, tracker=provider.tracker)
    session_dir = tmp_path / ".argus" / "roam" / "terminal-evidence-failure"

    session = roam(
        target="private-target.exe",
        provider=provider,
        adapter=adapter,
        budget=budget,
        session_dir=session_dir,
        project_dir=tmp_path,
        generate_regressions=False,
    )

    assert adapter.app.menu_open is True
    assert session.execution_status == "error"
    assert "ATES evidence failure" in session.stopped_reason
    assert (session_dir / "report.md").is_file()
    assert (session_dir / "session.json").is_file()

    report = (session_dir / "report.md").read_text(encoding="utf-8")
    session_json = json.loads((session_dir / "session.json").read_text(encoding="utf-8"))
    assert "action outcome unresolved after durable dispatch commit" in report
    assert "action outcome unresolved after durable dispatch commit" in session_json["stopped_reason"]

    recorder = seen["recorder"]
    assert recorder.failed is True
    assert isinstance(recorder.failure, OSError)
    assert "ACTION_EXECUTED persistence failure" in str(recorder.failure)


def test_roam_cli_returns_nonzero_for_outcome_unknown_without_findings(
    tmp_path, monkeypatch
):
    import argus.adapters
    import argus.cli as cli
    import argus.engine.roam as roam_module

    class FakeConfig:
        project_dir = tmp_path
        argus_dir = tmp_path / ".argus"

        def make_provider(self, tracker):
            return FakeProvider([])

        def make_budget(self, tracker, minutes, max_tokens):
            return Budget(max_tokens=241, tracker=tracker)

        def make_knowledge_store(self):
            return None

    session = RoamSession(target="fake.exe", provider="fake")
    session.execution_status = "outcome_unknown"
    session.stopped_reason = "action outcome unresolved after durable dispatch commit"
    session.tokens = {"total_tokens": 0, "calls": 0}

    monkeypatch.setattr(cli, "load_config", lambda: FakeConfig())
    monkeypatch.setattr(argus.adapters, "create_adapter", lambda _kind: FakeAdapter())
    monkeypatch.setattr(roam_module, "roam", lambda **_kwargs: session)
    monkeypatch.setattr(cli.time, "strftime", lambda _fmt: "20260814-230000")

    result = CliRunner().invoke(
        cli.main,
        ["roam", "fake.exe", "--no-memory", "--no-regressions"],
    )

    assert result.exit_code == 1
    assert "0 finding(s)" in result.output
