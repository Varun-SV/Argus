import json

from argus.engine.roam import roam
from argus.tokens import Budget
from tests.conftest import FakeAdapter, FakeProvider


def _action(**kw):
    return json.dumps(kw)


def test_roam_explores_and_reports_model_bug(tmp_path):
    provider = FakeProvider([
        _action(action="click", element_id=2, why="open File menu"),
        _action(action="report_bug", title="Save is greyed out", severity="medium",
                expected="Save enabled after typing", actual="Save disabled", why="looks wrong"),
        _action(action="type", text="x" * 50, element_id=1, why="long input edge case"),
    ])
    budget = Budget(max_tokens=120 * 3 + 1, tracker=provider.tracker)
    session_dir = tmp_path / "session"
    session = roam(
        target="fake.exe",
        provider=provider,
        adapter=FakeAdapter(),
        budget=budget,
        session_dir=session_dir,
    )

    assert len(session.findings) == 1
    finding = session.findings[0]
    assert finding.title == "Save is greyed out"
    assert finding.source == "model"
    assert len(session.actions) == 2  # click + type (report_bug is not an app action)
    assert session.tokens["total_tokens"] > 0

    report = (session_dir / "report.md").read_text()
    assert "Save is greyed out" in report
    assert "fake.exe" in report
    assert "## Session journal" in report
    assert (session_dir / "session.json").exists()
    regressions = list(session_dir.glob("regression-*.test.yaml"))
    assert len(regressions) == 1
    assert "process_running" in regressions[0].read_text()


def test_roam_detects_crash(tmp_path):
    provider = FakeProvider([
        _action(action="click", element_id=2, why="poke"),
        _action(action="click", element_id=1, why="poke again"),
    ])
    budget = Budget(max_seconds=30, tracker=provider.tracker)
    session = roam(
        target="fake.exe",
        provider=provider,
        adapter=FakeAdapter(crash_after=1),  # crashes after first action
        budget=budget,
        session_dir=tmp_path / "s",
    )
    assert session.stopped_reason == "target crashed"
    assert any(f.source == "crash" for f in session.findings)
    crash = next(f for f in session.findings if f.source == "crash")
    assert crash.severity == "high"


def test_roam_detects_error_dialog(tmp_path):
    adapter = FakeAdapter()
    adapter.app.dialogs = ["Error: could not save file"]
    provider = FakeProvider([_action(action="wait", seconds=0.01)])
    budget = Budget(max_tokens=121, tracker=provider.tracker)
    session = roam(
        target="fake.exe", provider=provider, adapter=adapter,
        budget=budget, session_dir=tmp_path / "s",
    )
    assert any(f.source == "dialog" for f in session.findings)


def test_roam_respects_stop_flag(tmp_path):
    provider = FakeProvider([_action(action="wait", seconds=0.01)] * 100)
    budget = Budget(max_seconds=60, tracker=provider.tracker)
    session = roam(
        target="fake.exe", provider=provider, adapter=FakeAdapter(),
        budget=budget, session_dir=tmp_path / "s",
        stop_flag=lambda: True,
    )
    assert session.stopped_reason == "stopped by user"
    assert session.actions == []


def test_roam_text_only_model_never_gets_images(tmp_path):
    provider = FakeProvider([_action(action="wait", seconds=0.01)] * 3, vision=False)
    budget = Budget(max_tokens=121 * 2, tracker=provider.tracker)
    roam(
        target="fake.exe", provider=provider, adapter=FakeAdapter(),
        budget=budget, session_dir=tmp_path / "s",
    )
    assert provider.calls, "model should have been consulted"
    assert all(c["images"] is None for c in provider.calls)
