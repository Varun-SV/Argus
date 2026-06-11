"""Tests for new runner features: CLI assertions, retry logic, dry-run."""
import json

import pytest

from argus.engine.runner import run_test, check_assertion
from argus.engine.spec import parse_spec, AssertStep
from argus.adapters.base import Observation
from tests.conftest import FakeAdapter, FakeProvider


def _action(**kw):
    return json.dumps(kw)


# ---------- CLI assertion checks ----------

class CLIFakeAdapter(FakeAdapter):
    """FakeAdapter that reports stdout/exit_code in observations."""

    def observe(self, include_screenshot=True):
        obs = super().observe(include_screenshot=include_screenshot)
        obs.stdout = self.app.text_content
        obs.exit_code = 0
        return obs


def test_stdout_contains_pass():
    step = AssertStep(assertion="stdout_contains", expected="hello")
    adapter = CLIFakeAdapter()
    adapter.app.text_content = "hello world"
    result = check_assertion(step, adapter)
    assert result.status == "pass"


def test_stdout_contains_fail():
    step = AssertStep(assertion="stdout_contains", expected="goodbye")
    adapter = CLIFakeAdapter()
    adapter.app.text_content = "hello world"
    result = check_assertion(step, adapter)
    assert result.status == "fail"


def test_exit_code_is_pass():
    step = AssertStep(assertion="exit_code_is", expected=0)
    adapter = CLIFakeAdapter()
    result = check_assertion(step, adapter)
    assert result.status == "pass"


def test_exit_code_is_fail():
    step = AssertStep(assertion="exit_code_is", expected=1)
    adapter = CLIFakeAdapter()
    result = check_assertion(step, adapter)
    assert result.status == "fail"


def test_url_contains_pass():
    step = AssertStep(assertion="url_contains", expected="example")
    adapter = FakeAdapter()
    obs_with_url = adapter.observe()
    obs_with_url.url = "http://example.com/path"
    # Patch observe to return url-bearing observation
    adapter.observe = lambda **kw: obs_with_url
    result = check_assertion(step, adapter)
    assert result.status == "pass"


# ---------- retry logic ----------

RETRY_SPEC = """\
name: retry test
target:
  adapter: desktop-gui
  launch: fake.exe
retries: 2
steps:
  - "Do the thing"
  - assert:
      process_running: true
teardown:
  - close
"""


def test_retry_marks_flaky_on_eventual_pass():
    spec = parse_spec(RETRY_SPEC)
    # First attempt fails, second passes
    provider = FakeProvider([
        _action(action="done", success=False, note="first try failed"),
        _action(action="done", success=True, note="retry succeeded"),
    ])
    result = run_test(spec, provider, FakeAdapter())
    nl_step = result.steps[0]
    assert nl_step.status == "pass"
    assert nl_step.flaky is True


def test_no_retry_by_default():
    spec = parse_spec(RETRY_SPEC.replace("retries: 2", "retries: 0"))
    provider = FakeProvider([
        _action(action="done", success=False, note="fails"),
    ])
    result = run_test(spec, provider, FakeAdapter())
    assert result.steps[0].status == "fail"
    assert result.steps[0].flaky is False


# ---------- cost estimation ----------

from argus.tokens import estimate_cost


def test_estimate_cost_known_model():
    cost = estimate_cost("gpt-4o", prompt_tokens=1000, completion_tokens=500)
    assert cost > 0


def test_estimate_cost_ollama_is_zero():
    assert estimate_cost("gemma3:9b", 9999, 9999) == 0.0


def test_estimate_cost_unknown_model_is_zero():
    assert estimate_cost("some-random-model-xyz", 100, 50) == 0.0
