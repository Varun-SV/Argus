"""Chat understanding for the desktop app: slash commands, LLM routing guards, drafts."""

from __future__ import annotations

import json

import pytest

from argus.gui import assistant
from argus.gui.assistant import IntentError, check_draft, parse_slash, route_with_llm, validate_intent
from tests.conftest import FakeProvider

TESTS = [
    {"file": "checkout.test.yaml", "name": "Checkout happy path", "adapter": "browser"},
    {"file": "notepad-smoke.test.yaml", "name": "Notepad smoke test", "adapter": "desktop-gui"},
]
CONTEXT = {"tests": TESTS, "last_target": "notepad.exe", "providers": ["ollama", "anthropic"]}


def test_non_slash_text_is_not_parsed():
    assert parse_slash("run checkout", TESTS) is None


@pytest.mark.parametrize("text,expected", [
    ("/help", {"intent": "help", "args": {}}),
    ("/run", {"intent": "run", "args": {"tests": "all"}}),
    ("/run all", {"intent": "run", "args": {"tests": "all"}}),
    ("/run checkout", {"intent": "run", "args": {"tests": ["checkout.test.yaml"]}}),
    ("/dry-run notepad", {"intent": "dry_run", "args": {"tests": ["notepad-smoke.test.yaml"]}}),
    ("/dry-run draft", {"intent": "dry_run", "args": {"tests": "draft"}}),
    ("/tokens", {"intent": "tokens", "args": {}}),
    ("/history", {"intent": "report", "args": {}}),
    ("/providers anthropic", {"intent": "switch_provider", "args": {"provider": "anthropic"}}),
    ("/env hyperv", {"intent": "environment", "args": {"environment": "capsule", "capsule_provider": "hyperv"}}),
    ("/env local", {"intent": "environment", "args": {"environment": "local"}}),
    ("/knowledge reset notepad.exe", {"intent": "knowledge", "args": {"action": "reset", "target": "notepad.exe"}}),
    ("/knowledge", {"intent": "knowledge", "args": {"action": "show", "target": ""}}),
    ("/watch stop", {"intent": "watch", "args": {"action": "stop"}}),
    ("/write login works", {"intent": "write_test", "args": {"description": "login works"}}),
])
def test_slash_commands(text, expected):
    assert parse_slash(text, TESTS, "notepad.exe") == expected


def test_slash_roam_parses_target_and_options():
    got = parse_slash('/roam "http://localhost:3000" --minutes 5 --no-memory', TESTS)
    assert got == {"intent": "roam", "args": {
        "target": "http://localhost:3000", "adapter": "browser", "minutes": 5.0, "memory": False}}


def test_slash_roam_defaults_to_last_target():
    got = parse_slash("/roam", TESTS, "notepad.exe")
    assert got["args"]["target"] == "notepad.exe"
    assert got["args"]["adapter"] == "desktop-gui"


def test_slash_errors_are_explained():
    with pytest.raises(IntentError):
        parse_slash("/run nothing-like-this", TESTS)
    with pytest.raises(IntentError):
        parse_slash("/frobnicate", TESTS)
    with pytest.raises(IntentError):
        parse_slash("/roam", TESTS, "")


@pytest.mark.parametrize("target,adapter", [
    ("http://localhost:3000", "browser"),
    ("localhost:8080/app", "browser"),
    ("./my-cli.sh", "cli"),
    ("python tool.py --check", "cli"),
    ("notepad.exe", "desktop-gui"),
])
def test_adapter_for(target, adapter):
    assert assistant.adapter_for(target) == adapter


def test_extract_json_tolerates_fences_and_prose():
    text = 'Sure!\n```json\n{"intent": "run", "args": {"tests": "all", "x": "a}b"}}\n```'
    assert assistant.extract_json(text) == {"intent": "run", "args": {"tests": "all", "x": "a}b"}}
    assert assistant.extract_json("no json here") is None


def test_llm_routing_runs_a_known_test():
    provider = FakeProvider([json.dumps({"intent": "run", "args": {"tests": ["checkout"], "environment": "capsule"}})])
    got = route_with_llm(provider, "please run the checkout test in a capsule", CONTEXT)
    assert got == {"intent": "run", "args": {"tests": ["checkout.test.yaml"], "environment": "capsule"}}
    sent = json.loads(provider.calls[0]["user"])
    assert sent["message"] == "please run the checkout test in a capsule"
    assert sent["context"]["tests"][0]["file"] == "checkout.test.yaml"


def test_llm_cannot_invent_tests():
    got = validate_intent({"intent": "run", "args": {"tests": ["drop-database.test.yaml"]}}, "run it", CONTEXT)
    assert got["intent"] == "chat"


def test_llm_roam_target_must_come_from_the_user():
    invented = validate_intent({"intent": "roam", "args": {"target": "rm -rf /"}}, "explore my app", CONTEXT)
    assert invented["intent"] == "chat"
    quoted = validate_intent({"intent": "roam", "args": {"target": "calc.exe", "minutes": 3}}, "roam calc.exe for 3 min", CONTEXT)
    assert quoted["args"] == {"target": "calc.exe", "adapter": "desktop-gui", "minutes": 3.0, "memory": None}
    again = validate_intent({"intent": "roam", "args": {"target": "notepad.exe"}}, "roam it again", CONTEXT)
    assert again["args"]["target"] == "notepad.exe"


def test_llm_roam_target_rejects_incidental_substrings():
    incidental = validate_intent(
        {"intent": "roam", "args": {"target": "sh", "adapter": "cli"}},
        "what should I test?",
        CONTEXT,
    )
    assert incidental["intent"] == "chat"

    path_fragment = validate_intent(
        {"intent": "roam", "args": {"target": "sh", "adapter": "cli"}},
        "roam /bin/sh for a minute",
        CONTEXT,
    )
    assert path_fragment["intent"] == "chat"

    explicit = validate_intent(
        {"intent": "roam", "args": {"target": "sh", "adapter": "cli"}},
        "roam sh for a minute",
        CONTEXT,
    )
    assert explicit["intent"] == "roam" and explicit["args"]["target"] == "sh"


def test_llm_roam_target_must_preserve_full_command_and_url():
    shortened_cli = validate_intent(
        {"intent": "roam", "args": {"target": "python cleanup.py", "adapter": "cli"}},
        "roam python cleanup.py --dry-run",
        CONTEXT,
    )
    assert shortened_cli["intent"] == "chat"

    full_cli = validate_intent(
        {"intent": "roam", "args": {"target": "python cleanup.py --dry-run", "adapter": "cli"}},
        "roam python cleanup.py --dry-run",
        CONTEXT,
    )
    assert full_cli["intent"] == "roam"

    positional = validate_intent(
        {"intent": "roam", "args": {"target": "python cleanup.py", "adapter": "cli"}},
        "roam python cleanup.py input.txt",
        CONTEXT,
    )
    assert positional["intent"] == "chat"

    shortened_url = validate_intent(
        {"intent": "roam", "args": {"target": "https://host/path", "adapter": "browser"}},
        "roam https://host/path?safe=true",
        CONTEXT,
    )
    assert shortened_url["intent"] == "chat"

    full_url = validate_intent(
        {"intent": "roam", "args": {"target": "https://host/path?safe=true", "adapter": "browser"}},
        "roam https://host/path?safe=true",
        CONTEXT,
    )
    assert full_url["intent"] == "roam"


def test_last_roam_target_requires_an_explicit_reference():
    unrelated = validate_intent(
        {"intent": "roam", "args": {"target": "notepad.exe"}},
        "what can Argus do?",
        CONTEXT,
    )
    assert unrelated["intent"] == "chat"

    question = validate_intent(
        {"intent": "roam", "args": {"target": "notepad.exe"}},
        "what does roam mean again?",
        CONTEXT,
    )
    assert question["intent"] == "chat"

    again = validate_intent(
        {"intent": "roam", "args": {"target": "notepad.exe"}},
        "roam it again",
        CONTEXT,
    )
    assert again["intent"] == "roam"


def test_llm_provider_switch_must_be_configured():
    assert validate_intent({"intent": "switch_provider", "args": {"provider": "openai"}}, "use openai", CONTEXT)["intent"] == "chat"
    assert validate_intent({"intent": "switch_provider", "args": {"provider": "anthropic"}}, "use anthropic", CONTEXT) == {
        "intent": "switch_provider", "args": {"provider": "anthropic"}}
    descriptive = validate_intent(
        {"intent": "switch_provider", "args": {"provider": "anthropic"}},
        "I use Anthropic for work; tell me what Argus supports",
        CONTEXT,
    )
    assert descriptive["intent"] == "chat"


def test_unknown_intent_and_non_json_become_chat():
    assert validate_intent({"intent": "format_disk"}, "x", CONTEXT)["intent"] == "chat"
    provider = FakeProvider(["I can run tests and roam apps."])
    got = route_with_llm(provider, "what are you?", CONTEXT)
    assert got == {"intent": "chat", "args": {"reply": "I can run tests and roam apps."}}


def test_llm_minutes_are_clamped():
    # The duration comes from the user's words and is clamped; a model-supplied value is ignored.
    got = validate_intent({"intent": "roam", "args": {"target": "a.exe"}}, "roam a.exe for 10000 minutes", CONTEXT)
    assert got["args"]["minutes"] == 240.0
    got = validate_intent({"intent": "roam", "args": {"target": "a.exe", "minutes": 10_000}}, "roam a.exe", CONTEXT)
    assert got["args"]["minutes"] is None


def test_roam_modifiers_come_from_the_user_not_the_model():
    text = "roam notepad.exe in a capsule without memory for 1 minute"
    dropped = validate_intent({"intent": "roam", "args": {"target": "notepad.exe"}}, text, CONTEXT)
    assert dropped["intent"] == "roam"
    assert dropped["args"]["environment"] == "capsule"
    assert "capsule_provider" not in dropped["args"]  # the session's Capsule provider applies
    assert dropped["args"]["memory"] is False
    assert dropped["args"]["minutes"] == 1.0

    # Modifiers the user didn't state are not invented by the model either.
    invented = validate_intent({"intent": "roam", "args": {
        "target": "notepad.exe", "environment": "local", "memory": True, "minutes": 99}},
        "roam notepad.exe", CONTEXT)
    assert invented["args"]["minutes"] is None and invented["args"]["memory"] is None
    assert "environment" not in invented["args"]

    local = validate_intent({"intent": "roam", "args": {"target": "notepad.exe", "environment": "capsule"}},
                            "roam notepad.exe for 30 seconds locally", CONTEXT)
    assert local["args"]["environment"] == "local" and local["args"]["minutes"] == 0.5

    again = validate_intent({"intent": "roam", "args": {"target": "notepad.exe"}},
                            "roam it again in a hyper-v capsule for 2 hours", CONTEXT)
    assert again["args"]["environment"] == "capsule"
    assert again["args"]["capsule_provider"] == "hyperv"
    assert again["args"]["minutes"] == 120.0


def test_model_classification_is_not_execution_authorization():
    # A router mistake must never turn a question into "run all".
    got = validate_intent({"intent": "run", "args": {}}, "what tests are available?", CONTEXT)
    assert got["intent"] == "chat"

    # The user's scope wins over a model-invented broader scope.
    got = validate_intent({"intent": "run", "args": {"tests": "all"}}, "please run checkout", CONTEXT)
    assert got == {"intent": "run", "args": {"tests": ["checkout.test.yaml"]}}

    # Other mutating intents also require an explicit request.
    assert validate_intent({"intent": "save_test"}, "what does saving do?", CONTEXT)["intent"] == "chat"
    assert validate_intent({"intent": "init"}, "what is init?", CONTEXT)["intent"] == "chat"
    assert validate_intent({"intent": "watch", "args": {"action": "start"}},
                           "what is watch mode?", CONTEXT)["intent"] == "chat"
    assert validate_intent({"intent": "watch", "args": {"action": "start"}},
                           "start watch for test changes", CONTEXT)["args"]["action"] == "start"


def test_negated_mutating_requests_never_authorize_actions():
    assert validate_intent(
        {"intent": "run", "args": {"tests": ["checkout"]}}, "don't run checkout", CONTEXT
    )["intent"] == "chat"
    assert validate_intent(
        {"intent": "roam", "args": {"target": "notepad.exe"}}, "don't roam notepad.exe", CONTEXT
    )["intent"] == "chat"
    assert validate_intent({"intent": "save_test"}, "don't save the draft", CONTEXT)["intent"] == "chat"
    assert validate_intent(
        {"intent": "knowledge", "args": {"action": "reset", "target": "notepad.exe"}},
        "don't reset knowledge for notepad.exe",
        CONTEXT,
    )["intent"] == "chat"
    assert validate_intent(
        {"intent": "switch_provider", "args": {"provider": "anthropic"}},
        "don't switch to anthropic",
        CONTEXT,
    )["intent"] == "chat"

    all_tests = validate_intent({"intent": "run", "args": {}}, "run all", CONTEXT)
    assert all_tests == {"intent": "run", "args": {"tests": "all"}}


def test_negated_local_wording_never_creates_a_local_override():
    got = validate_intent(
        {"intent": "run", "args": {"tests": ["checkout"]}},
        "don't run locally; run checkout",
        CONTEXT,
    )
    assert got["intent"] == "chat"
    assert got["args"].get("environment") != "local"

    # A test name/description containing the word is not an environment setting.
    settings, problem = assistant.run_settings_from_text("run the locally-cached checkout test")
    assert settings == {} and problem is None


def test_free_text_roam_adapter_comes_from_the_target_not_the_model():
    got = validate_intent(
        {"intent": "roam", "args": {"target": "https://host/app", "adapter": "cli"}},
        "roam https://host/app",
        CONTEXT,
    )
    assert got["intent"] == "roam"
    assert got["args"]["adapter"] == "browser"


def test_multi_word_desktop_targets_accept_natural_modifiers():
    vscode = validate_intent(
        {"intent": "roam", "args": {"target": "Visual Studio Code", "adapter": "cli"}},
        "roam Visual Studio Code for 5 minutes",
        CONTEXT,
    )
    assert vscode["intent"] == "roam"
    assert vscode["args"]["target"] == "Visual Studio Code"
    assert vscode["args"]["adapter"] == "desktop-gui"
    assert vscode["args"]["minutes"] == 5.0

    chrome = validate_intent(
        {"intent": "roam", "args": {"target": "Google Chrome"}},
        "explore Google Chrome without memory",
        CONTEXT,
    )
    assert chrome["intent"] == "roam"
    assert chrome["args"]["adapter"] == "desktop-gui"
    assert chrome["args"]["memory"] is False


def test_explain_requires_explicit_failure_explanation_request():
    informational = validate_intent({"intent": "explain"}, "what can Argus explain?", CONTEXT)
    assert informational["intent"] == "chat"
    unrelated = validate_intent({"intent": "explain"}, "show me the available tests", CONTEXT)
    assert unrelated["intent"] == "chat"
    explicit = validate_intent({"intent": "explain"}, "why did the last test fail?", CONTEXT)
    assert explicit == {"intent": "explain", "args": {}}
    advisory = validate_intent({"intent": "explain"}, "should you explain the last failure?", CONTEXT)
    assert advisory["intent"] == "chat"


def test_environment_intent_requires_an_explicit_picker_change():
    assert validate_intent(
        {"intent": "environment", "args": {"environment": "local"}},
        "what is local mode?",
        CONTEXT,
    )["intent"] == "chat"
    assert validate_intent(
        {"intent": "environment", "args": {"environment": "capsule"}},
        "Capsule mode isolates tests",
        CONTEXT,
    )["intent"] == "chat"
    assert validate_intent(
        {"intent": "environment", "args": {"environment": "local"}},
        "switch to local",
        CONTEXT,
    ) == {"intent": "environment", "args": {"environment": "local"}}
    assert validate_intent(
        {"intent": "environment", "args": {"environment": "local"}},
        "use a libvirt capsule",
        CONTEXT,
    ) == {"intent": "environment", "args": {"environment": "capsule", "capsule_provider": "libvirt"}}


def test_write_test_wording_cannot_save_an_existing_draft():
    context = dict(CONTEXT, has_draft=True)
    misclassified = validate_intent(
        {"intent": "save_test"},
        "write a test for login",
        context,
    )
    assert misclassified["intent"] == "chat"
    assert validate_intent({"intent": "save_test"}, "save that draft", context) == {
        "intent": "save_test", "args": {}}
    assert validate_intent({"intent": "save_test"}, "persist the current spec", context) == {
        "intent": "save_test", "args": {}}
    assert validate_intent({"intent": "save_test"}, "save money", context)["intent"] == "chat"


def test_advisory_should_you_run_is_not_execution_authorization():
    advisory = validate_intent(
        {"intent": "run", "args": {"tests": ["checkout"]}},
        "Should you run checkout?",
        CONTEXT,
    )
    assert advisory["intent"] == "chat"

    actual_request = validate_intent(
        {"intent": "run", "args": {"tests": ["checkout"]}},
        "Can you run checkout?",
        CONTEXT,
    )
    assert actual_request == {"intent": "run", "args": {"tests": ["checkout.test.yaml"]}}


GOOD_SPEC = """name: Search returns results
target:
  adapter: browser
  launch: "http://localhost:3000"
steps:
  - "Type 'notebook' into the search box and press Enter"
  - assert:
      text_visible: "results"
"""


def test_check_draft_accepts_valid_spec_and_names_file():
    draft = check_draft(GOOD_SPEC, existing=["search-returns-results.test.yaml"])
    assert draft["ok"] is True
    assert draft["file"] == "search-returns-results-2.test.yaml"
    assert draft["adapter"] == "browser"
    assert draft["steps"][1] == {"kind": "assert", "text": "text_visible: 'results'"}


@pytest.mark.parametrize("yaml_text,needle", [
    ("name: x\nsteps: [\n", "invalid YAML"),
    ("name: x\ntarget: {adapter: browser}\nsteps: ['a']\n", "target"),
    ("name: x\ntarget: {adapter: robot, launch: y}\nsteps: ['a']\n", "unknown adapter"),
    ("name: x\ntarget: {adapter: cli, launch: y}\nsteps: ['a']\nstaging: []\n", "staging"),
    ("name: x\ntarget: {adapter: cli, launch: y}\nretries: once\nsteps: ['a']\n", "invalid spec"),
    ("name: x\ntarget: {adapter: cli, launch: y}\nsteps: 5\n", "invalid spec"),
])
def test_check_draft_rejects_bad_specs(yaml_text, needle):
    draft = check_draft(yaml_text)
    assert draft["ok"] is False
    assert needle in draft["error"]


def test_draft_spec_strips_fences():
    provider = FakeProvider(["```yaml\n" + GOOD_SPEC + "```"])
    draft = assistant.draft_spec(provider, "search works")
    assert draft["ok"] and draft["yaml"].startswith("name: Search returns results")


def test_explain_failure_sends_compact_result():
    provider = FakeProvider(["Step 2 failed because the alert said declined."])
    result = {"test_file": "checkout.test.yaml", "status": "fail", "error": None,
              "steps": [{"index": 1, "kind": "assert", "text": "text_visible", "status": "fail",
                         "expected": "Order confirmed", "actual": "declined", "screenshot_path": "secret.png"}]}
    text = assistant.explain_failure(provider, result)
    assert "declined" in text
    sent = json.loads(provider.calls[0]["user"])
    assert "screenshot_path" not in sent["steps"][0]


def test_llm_run_with_an_unknown_test_runs_nothing():
    got = validate_intent({"intent": "run", "args": {"tests": ["checkout", "payment"]}},
                          "run checkout and payment", CONTEXT)
    assert got["intent"] == "chat"
    assert "payment" in got["args"]["reply"]


def test_unspecified_roam_memory_defers_to_the_app_toggle():
    assert parse_slash("/roam notepad.exe", TESTS)["args"]["memory"] is None
    assert parse_slash("/roam notepad.exe --no-memory", TESTS)["args"]["memory"] is False
    assert parse_slash("/roam notepad.exe --memory", TESTS)["args"]["memory"] is True
    llm = validate_intent({"intent": "roam", "args": {"target": "a.exe"}}, "roam a.exe", CONTEXT)
    assert llm["args"]["memory"] is None
    explicit = validate_intent({"intent": "roam", "args": {"target": "a.exe", "memory": False}},
                               "roam a.exe without memory", CONTEXT)
    assert explicit["args"]["memory"] is False


def test_slash_roam_keeps_multi_word_targets():
    got = parse_slash("/roam python tool.py --check --minutes 2", TESTS)
    assert got["args"]["target"] == "python tool.py --check"
    assert got["args"]["adapter"] == "cli"
    assert got["args"]["minutes"] == 2.0


def test_exact_test_stem_wins_over_substring_matches():
    tests = [{"file": "checkout.test.yaml", "name": "Checkout happy path"},
             {"file": "checkout-refund.test.yaml", "name": "Refund"}]
    assert assistant.resolve_tests("checkout", tests) == ["checkout.test.yaml"]
    assert parse_slash("/run checkout", tests)["args"]["tests"] == ["checkout.test.yaml"]
    assert assistant.resolve_tests("check", tests) == ["checkout.test.yaml", "checkout-refund.test.yaml"]


def test_slash_roam_keeps_windows_backslashes():
    got = parse_slash(r"/roam C:\Tools\app.exe --minutes 3", TESTS)
    assert got["args"]["target"] == r"C:\Tools\app.exe"
    assert got["args"]["adapter"] == "desktop-gui" and got["args"]["minutes"] == 3.0
    spaced = parse_slash(r'/roam "C:\Program Files\App\app.exe" --no-memory', TESTS)
    assert spaced["args"]["target"] == r"C:\Program Files\App\app.exe"  # bare executable path
    assert spaced["args"]["memory"] is False

    multi = parse_slash(r'/roam "C:\Program Files\App\app.exe" --safe-mode', TESTS)
    assert multi["args"]["target"] == r'"C:\Program Files\App\app.exe" --safe-mode'


def test_roam_modifiers_before_the_verb_are_honoured():
    got = validate_intent({"intent": "roam", "args": {"target": "notepad.exe"}},
                          "In a capsule, roam notepad.exe", CONTEXT)
    assert got["intent"] == "roam"
    assert got["args"]["environment"] == "capsule" and "capsule_provider" not in got["args"]
    got = validate_intent({"intent": "roam", "args": {"target": "notepad.exe"}},
                          "Without memory, roam notepad.exe for 2 minutes", CONTEXT)
    assert got["args"]["memory"] is False and got["args"]["minutes"] == 2.0
    # Conflicting or unreadable modifier wording runs nothing.
    for text in ("Locally, roam notepad.exe in a capsule", "run the local build and roam notepad.exe"):
        got = validate_intent({"intent": "roam", "args": {"target": "notepad.exe"}}, text, CONTEXT)
        assert got["intent"] == "chat", text


@pytest.mark.parametrize("text,target", [
    ("roam python deploy.py --region locally", "python deploy.py --region"),
    ("roam python tool.py without memory", "python tool.py"),
    ("roam python tool.py for 1 minute", "python tool.py"),
    ("roam python tool.py locally --fast", "python tool.py locally --fast"),
])
def test_command_arguments_that_look_like_modifiers_are_never_stripped(text, target):
    got = validate_intent({"intent": "roam", "args": {"target": target}}, text, CONTEXT)
    assert got["intent"] == "chat"
    assert "quotes" in got["args"]["reply"]


def test_quoted_commands_are_literal_and_modifiers_follow_the_quote():
    got = validate_intent({"intent": "roam", "args": {"target": "python tool.py for 1 minute"}},
                          'roam "python tool.py for 1 minute"', CONTEXT)
    assert got["intent"] == "roam" and got["args"]["target"] == "python tool.py for 1 minute"
    assert got["args"]["minutes"] is None
    got = validate_intent({"intent": "roam", "args": {"target": "python deploy.py --region locally"}},
                          'roam "python deploy.py --region locally" in a capsule for 1 minute', CONTEXT)
    assert got["args"]["target"] == "python deploy.py --region locally"
    assert got["args"]["environment"] == "capsule" and got["args"]["minutes"] == 1.0


def test_run_settings_come_from_the_user_not_the_model():
    # A Capsule session plus a model-invented "local" must not become a local run.
    got = validate_intent({"intent": "run", "args": {"tests": ["checkout"], "environment": "local",
                                                     "retain": True}}, "run checkout", CONTEXT)
    assert got["intent"] == "run"
    assert not {"environment", "capsule_provider", "retain"} & set(got["args"])
    got = validate_intent({"intent": "run", "args": {"tests": ["checkout"]}},
                          "run checkout in a hyper-v capsule and keep the failure capsule", CONTEXT)
    assert got["args"]["environment"] == "capsule" and got["args"]["capsule_provider"] == "hyperv"
    assert got["args"]["retain"] is True
    # Switching the session environment also needs the user's own words.
    got = validate_intent({"intent": "environment", "args": {"environment": "local"}}, "what can you do", CONTEXT)
    assert got["intent"] == "chat"
    got = validate_intent({"intent": "environment", "args": {"environment": "local"}},
                          "switch to a libvirt capsule", CONTEXT)
    assert got["args"] == {"environment": "capsule", "capsule_provider": "libvirt"}
