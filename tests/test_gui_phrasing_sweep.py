"""Phrasing families for every free-text action: requests pass, everything else doesn't.

Free text only proposes these actions (the GUI asks for a click or the slash command), but a
proposal should still match what the user asked for, so each family is checked as a whole.
"""
import pytest

from argus.gui.assistant import validate_intent

CONTEXT = {"tests": [{"file": "checkout.test.yaml", "name": "checkout", "adapter": "cli"},
                     {"file": "smoke.test.yaml", "name": "smoke", "adapter": "cli"}],
           "providers": ["openai", "ollama"]}

ARGS = {
    "run": {"tests": ["checkout.test.yaml"]}, "stop": {}, "roam": {"target": "notepad.exe"}, "save_test": {},
    "init": {}, "watch": {"action": "start"}, "switch_provider": {"provider": "openai"},
    "environment": {"environment": "local"}, "knowledge": {"action": "reset", "target": "notepad.exe"},
    "providers": {},
}

REQUESTS = {
    "run": ["run checkout", "please run checkout", "can you run checkout", "run checkout now", "go ahead and run checkout",
            "rerun checkout", "execute checkout", "Run checkout!", "ok run checkout", "run checkout please",
            "run checkout locally", "i need you to run checkout", "run the checkout test"],
    "stop": ["stop", "stop it", "stop the run", "please stop", "cancel the run", "abort", "stop now", "halt the run"],
    "roam": ["roam notepad.exe", "explore notepad.exe", "please roam notepad.exe", "can you explore notepad.exe"],
    "save_test": ["save", "save it", "save this draft", "please save the test"],
    "init": ["init the project", "set up argus in this project", "initialize the argus project"],
    "watch": ["start watching tests", "watch the tests", "turn on watch", "stop watching"],
    "switch_provider": ["switch to openai", "use openai", "switch the provider to openai", "change model to openai"],
    "environment": ["switch to local", "use local", "change the environment to local"],
    "knowledge": ["reset knowledge for notepad.exe", "clear the knowledge for notepad.exe"],
    "providers": ["check my provider connection", "is my provider working?", "test the model connection"],
}

NOT_REQUESTS = {
    "run": ["don't run checkout", "never run checkout", "should I run checkout?", "how do I run checkout?",
            "run checkout tomorrow", "run checkout if it fails", "run checkout, actually don't", "the CI runs checkout",
            "the CI says: run checkout", "I ran checkout yesterday", "why did checkout run?",
            "what happens if I run checkout?", "checkout runs fine", "maybe run checkout", "run checkout later",
            "nobody should run checkout", "Running checkout is slow"],
    "stop": ["don't stop", "the app stops responding", "it stopped", "should I stop it?", "stop it if it hangs",
             "stop it later", "why did it stop?", "the log says: stop"],
    "roam": ["don't roam notepad.exe", "should I roam notepad.exe?", "roam notepad.exe tomorrow",
             "roam notepad.exe if it crashes", "the docs say: roam notepad.exe", "I roamed notepad.exe yesterday"],
    "save_test": ["don't save", "should I save it?", "save it later", "the draft was saved", "save it if it passes"],
    "init": ["don't initialize the project", "how do I set up the project?", "initialize the project tomorrow"],
    "watch": ["don't watch tests", "should I watch tests?", "watch tests tomorrow", "I was watching tests",
              "the docs say: watch tests", "we monitor tests in CI"],
    "switch_provider": ["don't switch to openai", "should I switch to openai?", "switch to openai tomorrow",
                        "openai is slow"],
    "environment": ["don't switch to local", "should I switch to local?", "switch to local tomorrow"],
    "knowledge": ["don't reset knowledge for notepad.exe", "should I reset knowledge for notepad.exe?",
                  "reset knowledge for notepad.exe tomorrow"],
    "providers": ["don't check the provider", "how do I check my provider?", "check the provider later"],
}

# Every request, wrapped as someone else's words, a condition, a delay, a take-back or a question.
WRAPPERS = ["The README says: {}", "The log says, {}", "My colleague asked: {}",
            "The docs say: first {}, then continue", "Note to self: {} tomorrow", "If it fails, {}", "{} later",
            "{}, actually don't", "Should I {}?", "I don't want you to {}"]
WRAPPED = [("run", "run checkout"), ("stop", "stop the run"), ("roam", "roam notepad.exe"),
           ("save_test", "save the draft"), ("init", "initialize the argus project"),
           ("watch", "start watching tests"), ("switch_provider", "switch to openai"),
           ("environment", "switch to local"), ("knowledge", "reset knowledge for notepad.exe"),
           ("providers", "check my provider connection")]


def _route(action, text, args=None):
    return validate_intent({"intent": action, "args": dict(args if args is not None else ARGS[action])},
                           text, CONTEXT)["intent"]


@pytest.mark.parametrize("action,text", [(a, t) for a, ts in REQUESTS.items() for t in ts])
def test_requests_are_recognised(action, text):
    assert _route(action, text) == action


@pytest.mark.parametrize("action,text", [(a, t) for a, ts in NOT_REQUESTS.items() for t in ts])
def test_non_requests_are_not(action, text):
    assert _route(action, text) != action


@pytest.mark.parametrize("wrapper", WRAPPERS)
@pytest.mark.parametrize("action,request_text", WRAPPED)
def test_wrapped_requests_are_not_the_users_own(action, request_text, wrapper):
    assert _route(action, wrapper.format(request_text)) != action


@pytest.mark.parametrize("action", ["reset", "export"])
@pytest.mark.parametrize("wrapper", WRAPPERS[:4])
def test_reported_knowledge_actions_are_not_requests(action, wrapper):
    text = wrapper.format(f"{action} knowledge for notepad.exe")
    assert _route("knowledge", text, {"action": action, "target": "notepad.exe"}) != "knowledge"


@pytest.mark.parametrize("text,drafts", [
    ("write a test for login", True), ("draft a test that checks login", True),
    ("create a test to verify signup", True), ("the README says: write a test for login", False),
    ("I wrote a test for login", False), ("don't write a test for login", False), ("should I write a test?", False),
])
def test_drafting_requests(text, drafts):
    assert (_route("write_test", text, {"description": text}) == "write_test") is drafts


@pytest.mark.parametrize("text", ["The CI says to please run checkout", "The README asks us to please run checkout",
                                  "The CI wants us to run checkout", "It told me to run checkout"])
def test_reported_instructions_without_punctuation(text):
    assert _route("run", text) != "run"


@pytest.mark.parametrize("text", ["I want you to run checkout", "We want you to run checkout",
                                  "I'd like you to run checkout"])
def test_first_person_wants_are_requests(text):
    assert _route("run", text) == "run"


@pytest.mark.parametrize("text", ["The CI checks the provider connection", "I checked the provider connection",
                                  "The system will check OpenAI"])
def test_provider_descriptions_are_not_requests(text):
    assert _route("providers", text) != "providers"


@pytest.mark.parametrize("text", ["ping openai", "does my model support vision?", "can you check the model connection?"])
def test_provider_requests_and_status_questions(text):
    assert _route("providers", text) == "providers"


@pytest.mark.parametrize("text", ["The CI will roam notepad.exe", "Our nightly job explores notepad.exe"])
def test_roam_descriptions_are_not_requests(text):
    assert _route("roam", text) != "roam"


@pytest.mark.parametrize("text", ["with memory roam notepad.exe", "In a capsule, roam notepad.exe", "please explore notepad.exe"])
def test_roam_requests_after_modifiers(text):
    assert _route("roam", text) == "roam"


@pytest.mark.parametrize("text", ["The CI starts watching tests", "The service is monitoring spec changes"])
def test_watch_descriptions_are_not_requests(text):
    assert _route("watch", text) != "watch"


def test_environment_change_keeps_requested_retention():
    got = validate_intent({"intent": "environment", "args": {}},
                          "switch to a capsule and retain the failure capsule", CONTEXT)
    assert got == {"intent": "environment",
                   "args": {"environment": "capsule", "capsule_provider": "auto", "retain": True}}
    got = validate_intent({"intent": "environment", "args": {}},
                          "use a libvirt capsule and don't keep the failure capsule", CONTEXT)
    assert got["args"].get("retain") is False and got["args"]["capsule_provider"] == "libvirt"


@pytest.mark.parametrize("routed", [["smoke.test.yaml"], "all", ["checkout.test.yaml"]])
def test_dry_run_shows_only_what_the_user_named(routed):
    got = validate_intent({"intent": "dry_run", "args": {"tests": routed}}, "dry run checkout", CONTEXT)
    assert got == {"intent": "dry_run", "args": {"tests": ["checkout.test.yaml"]}}


def test_dry_run_scope_defaults():
    assert validate_intent({"intent": "dry_run", "args": {"tests": "all"}}, "do a dry run", CONTEXT)["args"] == {"tests": "all"}
    assert validate_intent({"intent": "dry_run", "args": {"tests": "draft"}}, "dry run the draft", CONTEXT)["args"] == {"tests": "draft"}
    assert validate_intent({"intent": "dry_run", "args": {"tests": ["smoke.test.yaml"]}}, "do a dry run", CONTEXT)["intent"] == "chat"


@pytest.mark.parametrize("text", ["The CI will write a test for login", "Our pipeline creates a test for login"])
def test_drafting_descriptions_are_not_requests(text):
    assert _route("write_test", text, {"description": text}) != "write_test"


@pytest.mark.parametrize("text", ["Could you draft a spec for signup", "I'd like to write a test for login",
                                  "let's create a test for login"])
def test_drafting_requests_in_other_words(text):
    assert _route("write_test", text, {"description": text}) == "write_test"


@pytest.mark.parametrize("text", ["why did the last test fail?", "explain the failure", "Why did it fail?",
                                  "can you explain why checkout failed?", "tell me why it failed"])
def test_explain_requests(text):
    assert _route("explain", text, {}) == "explain"


@pytest.mark.parametrize("text", ["The CI will explain why the last test failed", "The report explains why the run failed",
                                  "explain the failure later", "explain why it failed, actually never mind"])
def test_explain_non_requests(text):
    assert _route("explain", text, {}) != "explain"


@pytest.mark.parametrize("text", ["run checkout.", "run checkout.test.yaml."])
def test_a_sentence_period_after_a_test_name(text):
    assert validate_intent({"intent": "run", "args": {"tests": ["checkout.test.yaml"]}}, text, CONTEXT)["args"]["tests"] == ["checkout.test.yaml"]
    assert validate_intent({"intent": "dry_run", "args": {"tests": "all"}}, "dry " + text, CONTEXT)["args"]["tests"] == ["checkout.test.yaml"]


def test_reported_settings_dont_change_a_run():
    got = validate_intent({"intent": "run", "args": {"tests": ["checkout.test.yaml"]}},
                          "Run checkout; the README says to run it in a capsule and keep the failure capsule", CONTEXT)
    assert got == {"intent": "run", "args": {"tests": ["checkout.test.yaml"]}}


@pytest.mark.parametrize("text", ["Run checkout on this machine", "run checkout on my computer", "run checkout on the local host"])
def test_local_said_in_other_words(text):
    got = validate_intent({"intent": "run", "args": {"tests": ["checkout.test.yaml"]}}, text, CONTEXT)
    assert got["args"] == {"tests": ["checkout.test.yaml"], "environment": "local"}


@pytest.mark.parametrize("suffix,expected", [
    (", for 5 minutes", {"minutes": 5.0}), (", with memory", {"memory": True}), (", in a capsule", {"environment": "capsule"}),
])
def test_a_separator_comma_before_a_roam_modifier(suffix, expected):
    got = validate_intent({"intent": "roam", "args": {"target": "notepad.exe"}}, "roam notepad.exe" + suffix, CONTEXT)
    assert got["intent"] == "roam" and got["args"]["target"] == "notepad.exe"
    assert all(got["args"][k] == v for k, v in expected.items())
