"""Free-roam mode — autonomous exploratory testing.

``argus roam "<command>"`` launches the target application and lets the
LLM explore it like a curious child: clicking, typing, opening menus,
trying odd inputs — with no script. While roaming Argus:

  * **detects bugs** — crashes (process exit), error dialogs, hangs
    (observation failures), and anything the model itself flags as broken
    via the ``report_bug`` action;
  * **documents everything** — every action, observation summary and
    finding is appended to a session journal;
  * **captures evidence** — a screenshot is saved for every suspected bug;
  * **writes a report** — ``.argus/roam/<stamp>/report.md`` plus optional
    regression ``*.test.yaml`` stubs for each finding.

Budgets: Ollama sessions are time-bounded (local = free); paid providers
may use a time budget, a token budget, or both. Token usage is tracked
throughout and printed live.

Background/virtual display: on Windows the target runs on the real desktop
(Windows has no Xvfb equivalent — do not use the machine while roaming).
Linux virtual-display (Xvfb) support is on the roadmap.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

from argus.adapters.base import Adapter, AdapterError
from argus.engine.agent import ACTION_SCHEMA, extract_action
from argus.providers.base import LLMProvider, ProviderError
from argus.tokens import Budget

ROAM_SYSTEM_PROMPT = """\
You are Argus in FREE-ROAM mode: an endlessly curious explorer let loose on a
desktop application, like a child poking at everything to see what breaks.
Your mission: discover bugs, crashes, confusing UI and broken flows.

Behaviour:
- Explore broadly: open every menu, click every button, fill every field.
- Try edge cases: empty input, very long text, special characters, rapid
  repeated actions, cancel mid-operation.
- Note what you expected vs what happened.
- If something looks broken (error dialog, frozen UI, wrong result,
  data loss, garbled text), report it with:
  {"action":"report_bug","title":"...","severity":"low|medium|high",
   "expected":"...","actual":"...","why":"..."}
- After reporting, continue exploring something else.
- NEVER use {"action":"done"} — you roam until your budget runs out.

""" + ACTION_SCHEMA.replace(
    '{"action":"done","success":true|false,"note":"..."}',
    '{"action":"report_bug","title":"...","severity":"low|medium|high","expected":"...","actual":"...","why":"..."}',
)


@dataclass
class Finding:
    title: str
    severity: str
    expected: str
    actual: str
    detail: str
    at_action: int
    screenshot: Optional[str] = None  # relative path inside the session dir
    source: str = "model"             # model | crash | dialog | hang


@dataclass
class RoamSession:
    target: str
    provider: str
    started_at: float = field(default_factory=time.time)
    actions: List[dict] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    journal: List[str] = field(default_factory=list)
    tokens: dict = field(default_factory=dict)
    stopped_reason: str = ""
    duration_s: float = 0.0

    def log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.journal.append(f"[{stamp}] {line}")


def roam(
    target: str,
    provider: LLMProvider,
    adapter: Adapter,
    budget: Budget,
    session_dir: Path,
    on_event: Optional[Callable[[str], None]] = None,
    stop_flag: Optional[Callable[[], bool]] = None,
    generate_regressions: bool = True,
) -> RoamSession:
    session = RoamSession(target=target, provider=provider.describe())
    session_dir.mkdir(parents=True, exist_ok=True)
    shots_dir = session_dir / "shots"
    shots_dir.mkdir(exist_ok=True)

    def emit(line: str) -> None:
        session.log(line)
        if on_event:
            on_event(line)

    try:
        use_vision = provider.supports_vision()
    except ProviderError as exc:
        session.stopped_reason = f"provider check failed: {exc}"
        emit(session.stopped_reason)
        return session
    if not use_vision:
        emit(
            f"model '{provider.model}' is not multimodal — roaming on the "
            "accessibility tree only (no vision)."
        )

    emit(f"launching target: {target}")
    try:
        adapter.launch(target)
    except AdapterError as exc:
        session.stopped_reason = f"launch failed: {exc}"
        emit(session.stopped_reason)
        return session

    budget.restart()
    emit(f"roaming with budget: {budget.describe()}")
    started = time.monotonic()
    history: List[str] = []
    consecutive_failures = 0

    try:
        while True:
            if stop_flag and stop_flag():
                session.stopped_reason = "stopped by user"
                break
            reason = budget.exhausted()
            if reason:
                session.stopped_reason = reason
                break

            obs = adapter.observe(include_screenshot=use_vision)

            # ---- automatic bug detectors -------------------------------
            if not obs.process_alive:
                _add_finding(session, Finding(
                    title="Application crashed / exited unexpectedly",
                    severity="high",
                    expected="application keeps running during normal interaction",
                    actual=obs.error or "process exited",
                    detail=f"after action #{len(session.actions)}: "
                           + (history[-1] if history else "launch"),
                    at_action=len(session.actions),
                    source="crash",
                ), None, shots_dir, emit)
                session.stopped_reason = "target crashed"
                break

            for dialog in obs.dialogs:
                if _looks_like_error(dialog) and not _already_reported(session, dialog):
                    _add_finding(session, Finding(
                        title=f"Error dialog appeared: “{dialog}”",
                        severity="medium",
                        expected="no error dialogs during normal interaction",
                        actual=f"dialog “{dialog}” opened",
                        detail=f"after: {history[-1] if history else 'launch'}",
                        at_action=len(session.actions),
                        source="dialog",
                    ), obs.screenshot_png, shots_dir, emit)

            if obs.error:
                consecutive_failures += 1
                if consecutive_failures >= 3 and not _already_reported(session, "unresponsive"):
                    _add_finding(session, Finding(
                        title="Application appears unresponsive",
                        severity="high",
                        expected="UI stays observable",
                        actual=obs.error,
                        detail=f"3 consecutive observation failures at action #{len(session.actions)}",
                        at_action=len(session.actions),
                        source="hang",
                    ), obs.screenshot_png, shots_dir, emit)
            else:
                consecutive_failures = 0

            # ---- ask the model for the next poke -------------------------
            goal = (
                "Free-roam exploration. Explore the application, try edge cases, "
                "and report anything broken. Vary your actions; avoid repeating "
                "the same action twice in a row."
            )
            from argus.engine.agent import observation_prompt

            images = [obs.screenshot_png] if (use_vision and obs.screenshot_png) else None
            try:
                response = provider.chat(
                    system=ROAM_SYSTEM_PROMPT,
                    user=observation_prompt(obs, goal, history),
                    images=images,
                )
            except ProviderError as exc:
                session.stopped_reason = f"provider error: {exc}"
                emit(session.stopped_reason)
                break

            try:
                action = extract_action(response.text)
            except Exception as exc:
                emit(f"unparseable model reply ({exc}) — skipping turn")
                history.append("(model reply was not a valid action)")
                continue

            kind = action.get("action", "")
            if kind == "report_bug":
                _add_finding(session, Finding(
                    title=str(action.get("title", "Untitled finding")),
                    severity=str(action.get("severity", "medium")),
                    expected=str(action.get("expected", "")),
                    actual=str(action.get("actual", "")),
                    detail=str(action.get("why", "")),
                    at_action=len(session.actions),
                    source="model",
                ), obs.screenshot_png, shots_dir, emit)
                history.append(f"reported bug: {action.get('title')}")
                continue
            if kind == "done":  # the model is told not to, but tolerate it
                history.append("(model tried to stop — roaming continues)")
                continue

            try:
                note = adapter.act(action)
                session.actions.append({"action": action, "note": note})
                why = action.get("why", "")
                emit(f"#{len(session.actions)} {note}" + (f" — {why}" if why else ""))
                history.append(note)
            except AdapterError as exc:
                session.actions.append({"action": action, "error": str(exc)})
                emit(f"#{len(session.actions)} action failed: {exc}")
                history.append(f"{kind} FAILED: {exc}")

            tokens = provider.tracker.snapshot()
            if tokens["calls"] % 10 == 0 and on_event:
                on_event(f"tokens so far: {tokens['total_tokens']} ({tokens['calls']} calls)")
            time.sleep(0.4)
    finally:
        try:
            adapter.close()
        except Exception:
            pass

    session.duration_s = time.monotonic() - started
    session.tokens = provider.tracker.snapshot()
    emit(
        f"roam finished: {session.stopped_reason or 'budget reached'} · "
        f"{len(session.actions)} actions · {len(session.findings)} findings · "
        f"{session.tokens.get('total_tokens', 0)} tokens"
    )

    _write_report(session, session_dir)
    if generate_regressions and session.findings:
        _write_regressions(session, session_dir)
    _write_session_json(session, session_dir)
    return session


# ---------------------------------------------------------------------------


def _looks_like_error(title: str) -> bool:
    return bool(re.search(r"error|exception|crash|fail|warning|problem", title, re.I))


def _already_reported(session: RoamSession, key: str) -> bool:
    key = key.lower()
    return any(key in f.title.lower() or key in f.actual.lower() for f in session.findings)


def _add_finding(
    session: RoamSession,
    finding: Finding,
    screenshot_png: Optional[bytes],
    shots_dir: Path,
    emit: Callable[[str], None],
) -> None:
    if screenshot_png:
        name = f"finding-{len(session.findings) + 1:02d}.png"
        (shots_dir / name).write_bytes(screenshot_png)
        finding.screenshot = f"shots/{name}"
    session.findings.append(finding)
    emit(f"FINDING [{finding.severity}] {finding.title}")


def _write_report(session: RoamSession, session_dir: Path) -> Path:
    lines = [
        "# Argus free-roam report",
        "",
        f"- **Target:** `{session.target}`",
        f"- **Provider:** `{session.provider}`",
        f"- **Started:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(session.started_at))}",
        f"- **Duration:** {session.duration_s:.0f}s",
        f"- **Actions taken:** {len(session.actions)}",
        f"- **Findings:** {len(session.findings)}",
        f"- **Tokens used:** {session.tokens.get('total_tokens', 0)} "
        f"({session.tokens.get('calls', 0)} LLM calls)",
        f"- **Stopped because:** {session.stopped_reason or 'budget reached'}",
        "",
    ]
    if session.findings:
        lines.append("## Findings")
        lines.append("")
        for i, f in enumerate(session.findings, 1):
            lines += [
                f"### {i}. {f.title}",
                "",
                f"- **Severity:** {f.severity}",
                f"- **Detected by:** {f.source}",
                f"- **At action #:** {f.at_action}",
                f"- **Expected:** {f.expected or '—'}",
                f"- **Actual:** {f.actual or '—'}",
            ]
            if f.detail:
                lines.append(f"- **Detail:** {f.detail}")
            if f.screenshot:
                lines += ["", f"![finding {i}]({f.screenshot})"]
            lines.append("")
    else:
        lines += ["## Findings", "", "No bugs detected in this session.", ""]

    lines += ["## Session journal", "", "```"]
    lines += session.journal
    lines += ["```", ""]

    path = session_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _write_regressions(session: RoamSession, session_dir: Path) -> None:
    """Emit a regression-test stub per finding for the user to refine."""
    for i, f in enumerate(session.findings, 1):
        safe = re.sub(r"[^a-z0-9]+", "-", f.title.lower()).strip("-")[:40] or f"finding-{i}"
        stub = f"""\
# Auto-generated by argus roam — refine before relying on it.
# Finding: {f.title}
name: "regression: {f.title}"
target:
  adapter: desktop-gui
  launch: "{session.target}"

steps:
  - "Reproduce: {f.detail or f.actual or f.title}"
  - assert:
      process_running: true
  - assert:
      dialog_open: "Error"   # adjust: this asserts the bug IS present; invert once fixed

teardown:
  - close
"""
        (session_dir / f"regression-{i:02d}-{safe}.test.yaml").write_text(stub, encoding="utf-8")


def _write_session_json(session: RoamSession, session_dir: Path) -> None:
    data = {
        "target": session.target,
        "provider": session.provider,
        "started_at": session.started_at,
        "duration_s": session.duration_s,
        "actions": session.actions,
        "findings": [vars(f) for f in session.findings],
        "tokens": session.tokens,
        "stopped_reason": session.stopped_reason,
    }
    (session_dir / "session.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
