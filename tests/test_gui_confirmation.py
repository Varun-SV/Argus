"""Free text proposes state-changing actions; only a click or the slash command acts."""
import json

import pytest

from argus.config import ArgusConfig, init_project
from argus.gui import app as gui_app
from argus.gui.assistant import IntentError, confirmation, intent, needs_confirmation, parse_slash, to_slash
from tests.conftest import FakeProvider

TESTS = [{"file": "checkout.test.yaml", "name": "checkout", "adapter": "cli"},
         {"file": "smoke.test.yaml", "name": "smoke", "adapter": "cli"}]

ROUND_TRIP = [
    intent("run", tests="all"),
    intent("run", tests=["checkout.test.yaml"]),
    intent("run", tests=["checkout.test.yaml", "smoke.test.yaml"], environment="capsule",
           capsule_provider="hyperv", retain=True),
    intent("run", tests=["smoke.test.yaml"], environment="local"),
    intent("run", tests="all", environment="capsule", capsule_provider="auto", retain=False),
    intent("roam", target="notepad.exe", adapter="desktop-gui", minutes=5.0, memory=False),
    intent("roam", target="python -m http.server", adapter="cli", minutes=None, memory=None),
    intent("roam", target="C:\\Program Files\\App\\app.exe", adapter="desktop-gui", minutes=None, memory=True),
    intent("roam", target="https://example.test/a?q=1", adapter="browser", minutes=2.5, memory=None),
    intent("roam", target='say "hi"', adapter="cli", minutes=None, memory=None),
    intent("environment", environment="local"),
    intent("environment", environment="capsule", capsule_provider="libvirt", retain=False),
    intent("environment", environment="capsule", capsule_provider="auto"),
    intent("knowledge", action="reset", target="notepad.exe"),
    intent("knowledge", action="export", target="Visual Studio Code"),
    intent("switch_provider", provider="openai"),
    intent("watch", action="start"), intent("watch", action="stop"),
    intent("stop"), intent("save_test"), intent("init"), intent("providers"),
]


@pytest.mark.parametrize("routed", ROUND_TRIP, ids=lambda r: to_slash(r) or r["intent"])
def test_every_confirmed_action_has_an_exact_slash_command(routed):
    assert needs_confirmation(routed)
    assert parse_slash(to_slash(routed), TESTS) == routed


@pytest.mark.parametrize("routed", ROUND_TRIP, ids=lambda r: to_slash(r) or r["intent"])
def test_state_changing_free_text_comes_back_as_a_proposal(routed):
    proposal = confirmation(routed)
    assert proposal["intent"] == "confirm"
    assert proposal["args"]["proposed"] == routed and proposal["args"]["command"] == to_slash(routed)
    assert proposal["args"]["summary"]


@pytest.mark.parametrize("routed", [
    intent("help"), intent("tokens"), intent("report"), intent("evidence"), intent("explain"),
    intent("dry_run", tests="all"), intent("write_test", description="login works"),
    intent("environment"), intent("knowledge", action="show", target="notepad.exe"),
    intent("chat", reply="hi"),
])
def test_reading_explaining_and_drafting_stay_direct(routed):
    assert not needs_confirmation(routed) and confirmation(routed) is routed


def test_untransportable_roam_target_still_gets_a_proposal_without_a_command():
    routed = intent("roam", target="""say "it's" """.strip(), adapter="cli", minutes=None, memory=None)
    proposal = confirmation(routed)
    assert proposal["intent"] == "confirm" and proposal["args"]["command"] is None


@pytest.mark.parametrize("command", [
    "/run all --env", "/run all --env moon", "/run all --env local --retain", "/run all --retain --no-retain",
    "/run all --env local --env hyperv", "/env local --retain", "/env capsule --retain --no-retain",
    "/env capsule hyperv",
])
def test_slash_execution_flags_reject_contradictions(command):
    with pytest.raises(IntentError):
        parse_slash(command, TESTS)


def test_retain_alone_targets_a_capsule():
    assert parse_slash("/env --retain", TESTS) == intent("environment", environment="capsule", retain=True)


@pytest.fixture
def api(tmp_path, monkeypatch):
    for var in ("ARGUS_PROVIDER", "ARGUS_MODEL", "ARGUS_EXECUTION_ENVIRONMENT"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("ARGUS_GUI_STATE_DIR", str(tmp_path / "user-state"))
    init_project(tmp_path)
    for spec in (tmp_path / ".argus").glob("*.test.yaml"):
        spec.unlink()
    for name in ("checkout", "smoke"):
        (tmp_path / ".argus" / f"{name}.test.yaml").write_text(
            f"name: {name}\ntarget:\n  adapter: cli\n  launch: echo ok\nsteps:\n  - assert:\n      exit_code_is: 0\n",
            encoding="utf-8")
    return gui_app.ArgusAPI(tmp_path)


def test_free_text_run_is_proposed_and_starts_nothing(api, monkeypatch):
    provider = FakeProvider([json.dumps({"intent": "run", "args": {"tests": ["checkout.test.yaml"]}})])
    monkeypatch.setattr(ArgusConfig, "make_provider", lambda *args, **kwargs: provider)
    routed = api.interpret("run checkout")
    assert routed == {"intent": "confirm", "args": {
        "proposed": {"intent": "run", "args": {"tests": ["checkout.test.yaml"]}},
        "command": "/run checkout.test.yaml", "summary": "Run checkout.test.yaml"}}
    assert api._jobs == {}
    # Typing the proposed command is the confirmation, and it means the same thing.
    assert api.interpret(routed["args"]["command"]) == routed["args"]["proposed"]


def test_free_text_stop_never_stops_by_itself(api, monkeypatch):
    provider = FakeProvider([json.dumps({"intent": "stop", "args": {}})])
    monkeypatch.setattr(ArgusConfig, "make_provider", lambda *args, **kwargs: provider)
    assert api.interpret("stop the run")["intent"] == "confirm"
    assert not api._stop.is_set()


def test_slash_commands_still_act_directly(api):
    assert api.interpret("/stop") == intent("stop")
    assert api.interpret("/run smoke") == intent("run", tests=["smoke.test.yaml"])
