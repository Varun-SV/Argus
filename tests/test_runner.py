import json

from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from tests.conftest import FakeAdapter, FakeProvider

SPEC = """\
name: typing test
target:
  adapter: desktop-gui
  launch: fake.exe
steps:
  - "Type hello into the editor"
  - assert:
      text_visible: "hello"
  - assert:
      window_title_contains: "Fake App"
teardown:
  - close
"""


def _action(**kw):
    return json.dumps(kw)


def test_full_pass():
    spec = parse_spec(SPEC)
    provider = FakeProvider([
        _action(action="type", text="hello", element_id=1, why="type into editor"),
        _action(action="done", success=True, note="typed"),
    ])
    adapter = FakeAdapter()
    result = run_test(spec, provider, adapter)

    assert result.status == "pass"
    assert result.exit_code == 0
    assert [s.status for s in result.steps] == ["pass", "pass", "pass", "pass"]
    assert adapter.launched_with == "fake.exe"
    assert result.tokens["total_tokens"] > 0
    # assertions never consult the model
    assert all("assert" not in c["user"].lower().split("goal:")[0] for c in provider.calls)


def test_assertion_failure_skips_rest_but_runs_teardown():
    spec = parse_spec(SPEC)
    provider = FakeProvider([
        _action(action="type", text="goodbye", element_id=1),
        _action(action="done", success=True),
    ])
    result = run_test(spec, provider, FakeAdapter())

    assert result.status == "fail"
    assert result.exit_code == 1
    statuses = {s.text: s.status for s in result.steps}
    assert statuses["text_visible: 'hello'"] == "fail"
    # the later assert is skipped, teardown still runs
    assert [s.status for s in result.steps] == ["pass", "fail", "skipped", "pass"]
    failed = result.steps[1]
    assert failed.expected == "text_visible: 'hello'"
    assert "not found" in failed.actual


def test_nl_step_model_gives_up():
    spec = parse_spec(SPEC)
    provider = FakeProvider([
        _action(action="done", success=False, note="cannot find the editor"),
    ])
    result = run_test(spec, provider, FakeAdapter())
    assert result.steps[0].status == "fail"
    assert "cannot find" in result.steps[0].note


def test_launch_failure_is_error():
    spec = parse_spec(SPEC)

    class BrokenAdapter(FakeAdapter):
        def launch(self, target):
            from argus.adapters.base import AdapterError
            raise AdapterError("no such exe")

    result = run_test(spec, FakeProvider([]), BrokenAdapter())
    assert result.status == "error"
    assert result.exit_code == 2
    assert "launch failed" in result.error


def test_text_only_model_warns_and_runs():
    spec = parse_spec(SPEC)
    provider = FakeProvider([
        _action(action="type", text="hello", element_id=1),
        _action(action="done", success=True),
    ], vision=False)
    warnings = []
    result = run_test(spec, provider, FakeAdapter(), warn=warnings.append)
    assert result.status == "pass"
    assert any("not multimodal" in w for w in warnings)
    # no screenshots were sent to a text-only model
    assert all(c["images"] is None for c in provider.calls)


def test_unparseable_reply_is_error():
    spec = parse_spec(SPEC)
    provider = FakeProvider(["click the thing please"] * 12)
    result = run_test(spec, provider, FakeAdapter())
    assert result.steps[0].status == "error"
