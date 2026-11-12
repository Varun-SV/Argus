import json
import os
import stat
import sys

import pytest

from argus2.engine import Engine, quick_spec
from argus2.llm import Client, LLMError, extract_json
from argus2.settings import Settings, SettingsError
from argus2.spec import SpecError, parse_spec, placeholders
from argus2.store import StoreError, Workspace
from argus2.targets import CliTarget, SetupError, TargetError

PY = f'"{sys.executable}"'


# ---- spec -----------------------------------------------------------------
def test_spec_parses_every_step_kind():
    spec = parse_spec("""
name: demo
target: {type: cli, command: python}
steps:
  - Say hello
  - do: Say goodbye
  - verify: It was polite
  - run: echo hi
  - run: {command: echo hi, timeout: 5}
  - send: print(1)
  - wait: 0.5
  - check: {output_contains: hi, exit_code: 0}
""")
    assert [s.kind for s in spec.steps] == ["do", "do", "verify", "run", "run", "send", "wait", "check"]
    assert spec.needs_model


@pytest.mark.parametrize("text, message", [
    ("name: x\ntarget: {type: web}\nsteps: [a]", "needs a 'url'"),
    ("name: x\ntarget: {type: web, url: example.com}\nsteps: [a]", "must start with"),
    ("name: x\ntarget: {type: cli}\nsteps: [{click: a}]", "only works with a web target"),
    ("name: x\ntarget: {type: cli}\nsteps: [{send: a}]", "needs a 'command'"),
    ("name: x\ntarget: {type: cli}\nsteps: [{check: {text_visible: a}}]", "unknown check"),
    ("name: x\ntarget: {type: cli}\nsteps: [{check: {exit_code: '0'}}]", "whole number"),
    ("name: x\ntarget: {type: cli}\nsteps: []", "at least one step"),
    ("target: {type: cli}\nsteps: [a]", "Give the test a 'name'"),
    ("name: [", "not valid YAML"),
    ("name: x\ntarget: {type: desktop}\nsteps: [a]", "'web' or 'cli'"),
])
def test_spec_errors_say_what_to_fix(text, message):
    with pytest.raises(SpecError, match=message):
        parse_spec(text)


def test_placeholders_are_detected():
    spec = parse_spec("name: x\ntarget: {type: cli}\nsteps:\n  - check: {output_contains: '[EXPECTED TEXT]'}")
    assert placeholders(spec) == ["[EXPECTED TEXT]"]


def test_quick_spec_round_trips():
    spec = parse_spec(quick_spec({"type": "web", "url": "https://example.com"}, "- Open the menu\n\nIt lists three items"))
    assert [s.value for s in spec.steps] == ["Open the menu", "It lists three items"]
    with pytest.raises(SpecError):
        quick_spec({"type": "cli", "command": ""}, "  \n")


# ---- settings -------------------------------------------------------------
def test_keys_are_saved_privately_and_never_shown(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    settings = Settings(tmp_path / "s.json")
    assert "no API key" in settings.provider_ready("openai")
    settings.set_key("openai", "sk-secret-123456")
    assert settings.provider_ready("openai") == ""
    public = json.dumps(settings.public())
    assert "sk-secret-123456" not in public and "3456" in public
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(tmp_path / "s.json").st_mode) == 0o600
    assert Settings(tmp_path / "s.json").resolve("openai")["api_key"] == "sk-secret-123456"
    settings.set_key("openai", "")
    assert Settings(tmp_path / "s.json").resolve("openai")["api_key"] == ""


def test_environment_key_is_used_and_labelled(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "from-env")
    info = Settings(tmp_path / "s.json").resolve("gemini")
    assert (info["api_key"], info["key_source"]) == ("from-env", "environment")


def test_settings_validation_and_recovery(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{broken")
    settings = Settings(path)
    assert settings.load_error and settings.data["provider"] == ""
    with pytest.raises(SettingsError):
        settings.update({"provider": "nope"})
    with pytest.raises(SettingsError):
        settings.update({"max_actions": 0})
    with pytest.raises(SettingsError):
        settings.set_base_url("custom", "localhost:1234")
    with pytest.raises(SettingsError):
        settings.set_key("openai", "two words")
    assert "No model is set up" in settings.model_ready()


# ---- model clients --------------------------------------------------------
def test_openai_compatible_chat_models_and_errors(settings, model):
    client = Client(settings.resolve("custom"), "fake-1")
    assert client.list_models() == ["fake-1", "fake-2"]
    model.script("hello")
    reply = client.chat("sys", "hi", [(b"img", "image/jpeg")])
    assert reply.text == "hello" and client.usage.as_dict() == {"input": 100, "output": 10, "calls": 1}
    sent = model.requests[-1]["body"]["messages"][1]["content"]
    assert sent[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    settings.set_key("custom", "wrong")
    with pytest.raises(LLMError, match="rejected the API key"):
        Client(settings.resolve("custom"), "fake-1").chat("s", "u")
    settings.set_base_url("custom", "http://127.0.0.1:9/v1")
    with pytest.raises(LLMError, match="Could not connect"):
        Client(settings.resolve("custom"), "fake-1").list_models()


def test_anthropic_sdk_chat_models_and_errors(settings, model):
    settings.set_base_url("anthropic", model.url)
    settings.set_key("anthropic", model.key)
    client = Client(settings.resolve("anthropic"), "claude-opus-5-5")
    assert "fake-1" in client.list_models()
    model.script("pong")
    assert client.chat("sys", "ping", [(b"img", "image/jpeg")]).text == "pong"
    body = model.requests[-1]["body"]
    assert body["model"] == "claude-opus-5-5" and body["system"] == "sys"
    assert body["messages"][0]["content"][0]["type"] == "image"
    assert "temperature" not in body and "thinking" not in body
    settings.set_key("anthropic", "wrong")
    with pytest.raises(LLMError, match="rejected the API key"):
        Client(settings.resolve("anthropic"), "claude-opus-5-5").chat("s", "u")


def test_extract_json_tolerates_chatter():
    assert extract_json('Sure!\n```json\n{"action": "click", "ref": 3, "why": "a } brace"}\n```') == \
        {"action": "click", "ref": 3, "why": "a } brace"}
    assert extract_json("no json here") is None


# ---- command-line target --------------------------------------------------
def test_cli_run_and_checks(workspace):
    target = CliTarget({"command": "", "cwd": ""}, workspace)
    target.start()
    assert "exit code 0" in target.act({"action": "run", "command": "echo hello"})
    assert target.check("output_contains", "hello")[0] and target.check("exit_code", 0)[0]
    assert not target.check("output_absent", "hello")[0]
    target.act({"action": "run", "command": f'{PY} -c "import sys; sys.exit(3)"'})
    assert target.check("exit_code", 3)[0]
    with pytest.raises(TargetError, match="did not finish"):
        target.act({"action": "run", "command": f'{PY} -c "import time; time.sleep(5)"', "timeout": 1})
    with pytest.raises(TargetError, match="no open program"):
        target.act({"action": "send", "text": "x"})


def test_cli_interactive_session(workspace):
    target = CliTarget({"command": f"{PY} -u -i -q", "cwd": ""}, workspace)
    target.start()
    try:
        target.act({"action": "send", "text": "print(6 * 7)"})
        assert target.check("output_contains", "42")[0]
        target.act({"action": "send", "text": "x = 'kept'"})
        target.act({"action": "send", "text": "print(x * 2)"})
        assert target.check("output_contains", "keptkept")[0]
        assert target.check("transcript_contains", "42")[0]
        target.act({"action": "send", "text": "exit()"})
        with pytest.raises(TargetError, match="has exited"):
            target.act({"action": "send", "text": "print(1)"})
    finally:
        target.close()


def test_cli_setup_errors(workspace):
    with pytest.raises(SetupError, match="does not exist"):
        CliTarget({"command": "", "cwd": "missing"}, workspace).start()
    with pytest.raises(SetupError, match="exited straight away"):
        CliTarget({"command": f'{PY} -c "import sys; sys.exit(2)"', "cwd": ""}, workspace).start()


# ---- engine ---------------------------------------------------------------
def run(settings, workspace, text, stop=None):
    ws = Workspace(workspace)
    out = ws.new_result_dir()
    events = []
    engine = Engine(settings, workspace, lambda kind, **data: events.append((kind, data)), stop)
    result = engine.run_test(parse_spec(text), out)
    ws.save_result(out, result)
    return result, events


def test_exact_test_needs_no_model(tmp_path, workspace):
    blank = Settings(tmp_path / "blank.json")
    result, events = run(blank, workspace, """
name: echo
target: {type: cli}
steps:
  - run: echo hello from argus
  - check: {output_contains: hello from argus, exit_code: 0}
""")
    assert result["status"] == "passed" and result["tokens"]["calls"] == 0 and result["model"] == ""
    assert [e[0] for e in events][0] == "test_start" and events[-1][0] == "test_end"
    assert (workspace / ".argus" / "results" / result["id"] / "transcript.txt").read_text().startswith("$ echo")


def test_failed_check_stops_and_skips_the_rest(settings, workspace):
    result, _ = run(settings, workspace, """
name: echo
target: {type: cli}
steps:
  - run: echo hello
  - check: {output_contains: goodbye}
  - run: echo never
""")
    assert result["status"] == "failed" and "Step 2 failed" in result["summary"]
    assert [s["status"] for s in result["steps"]] == ["passed", "failed", "skipped"]


def test_model_steps_act_then_judge(settings, workspace, model):
    model.script({"action": "run", "command": "echo 4", "why": "compute"},
                 {"action": "done", "success": True, "why": "it printed 4"},
                 {"pass": True, "why": "the output shows 4"})
    result, events = run(settings, workspace, """
name: maths
target: {type: cli}
steps:
  - do: Print the number four
  - verify: The output shows 4
""")
    assert result["status"] == "passed" and result["tokens"]["calls"] == 3
    step = result["steps"][0]
    assert step["actions"][0]["label"] == "Run: echo 4" and step["judged_by"] == "model"
    assert {"observe", "decide", "act", "verify"} <= {d["phase"] for k, d in events if k == "phase"}
    assert "Output of the last action:\n4" in model.requests[1]["body"]["messages"][1]["content"]


def test_model_reporting_failure_fails_the_step(settings, workspace, model):
    model.script({"action": "done", "success": False, "why": "the program crashed"})
    result, _ = run(settings, workspace, "name: x\ntarget: {type: cli}\nsteps: [Do it]")
    assert result["status"] == "failed" and "crashed" in result["summary"]


def test_action_budget_and_bad_json_are_reported(settings, workspace, model):
    settings.update({"max_actions": 2})
    model.script({"action": "wait", "seconds": 0, "why": "a"}, {"action": "wait", "seconds": 0, "why": "b"})
    result, _ = run(settings, workspace, "name: x\ntarget: {type: cli}\nsteps: [Do it]")
    assert result["status"] == "failed" and "Not finished after 2 actions" in result["summary"]
    model.script("nope", "still nope", "never json")
    result, _ = run(settings, workspace, "name: x\ntarget: {type: cli}\nsteps: [Do it]")
    assert result["status"] == "error" and "not JSON" in result["summary"]


def test_missing_model_is_an_error_before_anything_runs(tmp_path, workspace):
    result, _ = run(Settings(tmp_path / "blank.json"), workspace,
                    "name: x\ntarget: {type: cli}\nsteps: [Do it, {run: echo hi}]")
    assert result["status"] == "error" and "No model is set up" in result["summary"]
    assert [s["status"] for s in result["steps"]] == ["skipped", "skipped"]


def test_placeholders_block_a_run(settings, workspace):
    result, _ = run(settings, workspace,
                    "name: x\ntarget: {type: cli}\nsteps:\n  - run: echo hi\n  - check: {output_contains: '[EXPECTED TEXT]'}")
    assert result["status"] == "error" and "placeholders" in result["summary"]


def test_stop_is_reported_as_stopped(settings, workspace):
    import threading
    stop = threading.Event()
    stop.set()
    result, _ = run(settings, workspace, "name: x\ntarget: {type: cli}\nsteps: [{run: echo hi}]", stop)
    assert result["status"] == "stopped"


def test_explore_reports_findings_and_what_was_exercised(settings, workspace, model):
    model.script({"action": "run", "command": "echo ok", "why": "try it"},
                 {"action": "finding", "severity": "high", "title": "Crash on empty input", "detail": "it crashed"},
                 {"action": "finding", "severity": "weird", "title": "Crash on empty input", "detail": "duplicate"},
                 {"action": "done", "why": "seen enough"})
    ws = Workspace(workspace)
    out = ws.new_result_dir()
    result = Engine(settings, workspace).explore({"type": "cli", "command": "", "cwd": ""}, "", 10, out)
    assert result["status"] == "completed" and result["summary"] == "1 finding after 1 action."
    assert result["findings"][0]["severity"] == "high" and "seen enough" in result["end_reason"]
    model.script({"action": "done", "why": "nothing to do"})
    result = Engine(settings, workspace).explore({"type": "cli", "command": "", "cwd": ""}, "", 10, ws.new_result_dir())
    assert "Nothing was exercised" in result["summary"]


def test_draft_is_validated_and_retried(settings, workspace, model):
    model.script("```yaml\nname: x\nsteps: []\n```",
                 "name: Greets\ntarget: {type: cli}\nsteps:\n  - run: echo hi\n  - verify: It greets the user")
    out = Engine(settings, workspace).draft("it greets", {"type": "cli", "command": "", "cwd": ""})
    assert out["name"] == "Greets" and "rejected" in model.requests[-1]["body"]["messages"][1]["content"]
    model.script("name: x\ntarget: {type: cli}\nsteps:\n  - check: {output_contains: '[EXPECTED TEXT]'}",
                 "name: x\ntarget: {type: cli}\nsteps:\n  - check: {output_contains: 'TODO'}")
    with pytest.raises(LLMError, match="placeholders"):
        Engine(settings, workspace).draft("something")


# ---- store ----------------------------------------------------------------
def test_workspace_tests_and_results(workspace):
    ws = Workspace(workspace)
    assert ws.add_samples() and len(ws.list_tests()) == 2
    with pytest.raises(StoreError):
        ws.read_test("../../etc/passwd")
    with pytest.raises(SpecError):
        ws.write_test("bad.yaml", "name: x")
    assert ws.free_name("The echo command!") == "the-echo-command.yaml"
    (ws.tests_dir / "broken.yaml").write_text("name: [")
    assert [t["error"] != "" for t in ws.list_tests()] == [True, False, False]
    with pytest.raises(StoreError):
        ws.get_result("../x")
    with pytest.raises(StoreError):
        ws.artifact("20260101-000000-abcdef", "../result.json")
