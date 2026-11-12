"""The test file format.

    name: Login rejects a wrong password
    target:
      type: web                 # web | cli
      url: https://example.com/login
    steps:
      - do: Sign in as "ann" with the password "wrong"     # the model acts
      - check: {text_visible: "Invalid password"}           # exact, no model
      - verify: An error is shown next to the password box  # the model judges

A plain string step means ``do``. Steps that need no model at all:
web ``goto``, ``click``, ``fill``, ``press``; cli ``run``, ``send``; both ``wait``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List

import yaml

WEB_CHECKS = ("text_visible", "text_absent", "url_contains", "title_contains",
              "element_visible", "element_absent")
CLI_CHECKS = ("output_contains", "output_absent", "output_matches", "exit_code",
              "transcript_contains")
WEB_STEPS = ("goto", "click", "fill", "press")
CLI_STEPS = ("run", "send")
MODEL_STEPS = ("do", "verify")
COMMON_STEPS = ("check", "wait")

REFERENCE = """\
name: A short sentence saying what this test proves
target:
  type: web            # web | cli
  url: https://...     # web: the page to open
  # command: python    # cli (optional): a program to keep open and talk to
  # cwd: .             # cli (optional): folder to run in, relative to the workspace
steps:
  # Steps the model performs or judges (need a model in Settings):
  - do: Describe what a user would do, in plain words
  - verify: Describe what should be true now, in plain words
  # Exact steps (no model needed) — web:
  - goto: https://...
  - click: "text=Sign in"          # any Playwright selector
  - fill: {selector: "#email", text: "a@b.c"}
  - press: Enter
  # Exact steps — cli:
  - run: echo hello                # one command, waits for it to finish
  - send: print(1 + 1)             # a line typed into the open program
  # Both:
  - wait: 1.5
  - check:
      # web: text_visible, text_absent, url_contains, title_contains,
      #      element_visible, element_absent
      # cli: output_contains, output_absent, output_matches (regex),
      #      exit_code, transcript_contains
      text_visible: Welcome
"""


class SpecError(ValueError):
    pass


@dataclass
class Step:
    kind: str
    value: Any
    text: str  # how the step is shown to people


@dataclass
class Spec:
    name: str
    target: Dict[str, Any]
    steps: List[Step] = field(default_factory=list)

    @property
    def needs_model(self) -> bool:
        return any(step.kind in MODEL_STEPS for step in self.steps)


def parse_target(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise SpecError("'target' must say what to test, for example:\n  target:\n    type: web\n    url: https://example.com")
    kind = str(raw.get("type") or "").strip().lower()
    if not kind:
        kind = "web" if raw.get("url") else "cli" if "command" in raw else ""
    if kind not in ("web", "cli"):
        raise SpecError("target.type must be 'web' or 'cli'.")
    unknown = set(raw) - {"type", "url", "command", "cwd"}
    if unknown:
        raise SpecError(f"Unknown target setting: {', '.join(sorted(unknown))}.")
    target: Dict[str, Any] = {"type": kind}
    if kind == "web":
        url = str(raw.get("url") or "").strip()
        if not url:
            raise SpecError("A web target needs a 'url'.")
        if not re.match(r"^(https?|file)://", url, re.I):
            raise SpecError(f"target.url must start with http://, https:// or file:// (got '{url}').")
        target["url"] = url
    else:
        command = raw.get("command")
        if command is not None and not isinstance(command, str):
            raise SpecError("target.command must be text.")
        target["command"] = (command or "").strip()
        cwd = raw.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise SpecError("target.cwd must be a folder path.")
        target["cwd"] = (cwd or "").strip()
    return target


def _text(value: Any, what: str, number: int) -> str:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool) or not str(value).strip():
        raise SpecError(f"Step {number}: '{what}' needs some text.")
    return str(value).strip()


def _parse_check(value: Any, target_type: str, number: int) -> Dict[str, Any]:
    allowed = WEB_CHECKS if target_type == "web" else CLI_CHECKS
    if not isinstance(value, dict) or not value:
        raise SpecError(f"Step {number}: 'check' needs at least one condition, e.g. {allowed[0]}: ...")
    out: Dict[str, Any] = {}
    for key, expected in value.items():
        if key not in allowed:
            raise SpecError(f"Step {number}: unknown check '{key}' for a {target_type} target. "
                            f"Available: {', '.join(allowed)}.")
        if key == "exit_code":
            if isinstance(expected, bool) or not isinstance(expected, int):
                raise SpecError(f"Step {number}: exit_code must be a whole number.")
            out[key] = expected
        else:
            out[key] = _text(expected, key, number)
            if key == "output_matches":
                try:
                    re.compile(out[key])
                except re.error as exc:
                    raise SpecError(f"Step {number}: output_matches is not a valid pattern ({exc}).") from None
    return out


def _parse_step(raw: Any, target: Dict[str, Any], number: int) -> Step:
    kind_of_target = target["type"]
    if isinstance(raw, str):
        return Step("do", _text(raw, "do", number), raw.strip())
    if not isinstance(raw, dict) or len(raw) != 1:
        raise SpecError(f"Step {number} must be a sentence or one of: do, verify, check, "
                        "run, send, goto, click, fill, press, wait.")
    kind, value = next(iter(raw.items()))
    kind = str(kind)
    allowed = MODEL_STEPS + COMMON_STEPS + (WEB_STEPS if kind_of_target == "web" else CLI_STEPS)
    if kind not in allowed:
        other = WEB_STEPS + CLI_STEPS
        if kind in other:
            raise SpecError(f"Step {number}: '{kind}' only works with a "
                            f"{'web' if kind in WEB_STEPS else 'cli'} target.")
        raise SpecError(f"Step {number}: unknown step '{kind}'. Available: {', '.join(allowed)}.")
    if kind in MODEL_STEPS:
        text = _text(value, kind, number)
        return Step(kind, text, text if kind == "do" else f"Verify: {text}")
    if kind == "check":
        checks = _parse_check(value, kind_of_target, number)
        return Step(kind, checks, "Check " + ", ".join(f"{k}: {v!r}" for k, v in checks.items()))
    if kind == "wait":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 300:
            raise SpecError(f"Step {number}: 'wait' needs a number of seconds between 0 and 300.")
        return Step(kind, float(value), f"Wait {value}s")
    if kind == "fill":
        if not isinstance(value, dict) or set(value) != {"selector", "text"}:
            raise SpecError(f"Step {number}: 'fill' needs both selector and text, "
                            "e.g. fill: {selector: \"#email\", text: \"a@b.c\"}")
        selector = _text(value["selector"], "fill.selector", number)
        return Step(kind, {"selector": selector, "text": str(value["text"] if value["text"] is not None else "")},
                    f"Fill {selector} with {str(value['text'])!r}")
    if kind == "run":
        if isinstance(value, dict):
            if set(value) - {"command", "timeout"} or "command" not in value:
                raise SpecError(f"Step {number}: 'run' takes a command and an optional timeout.")
            timeout = value.get("timeout", 60)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
                raise SpecError(f"Step {number}: run.timeout must be seconds between 1 and 3600.")
            command = _text(value["command"], "run.command", number)
            return Step(kind, {"command": command, "timeout": float(timeout)}, f"Run: {command}")
        command = _text(value, "run", number)
        return Step(kind, {"command": command, "timeout": 60.0}, f"Run: {command}")
    if kind == "send":
        if not target.get("command"):
            raise SpecError(f"Step {number}: 'send' types into an open program, so the target needs a 'command'.")
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            raise SpecError(f"Step {number}: 'send' needs the text to type.")
        return Step(kind, str(value), f"Type: {value}")
    text = _text(value, kind, number)
    label = {"goto": "Open", "click": "Click", "press": "Press"}[kind]
    return Step(kind, text, f"{label} {text}")


def parse_spec(content: str) -> Spec:
    try:
        raw = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark else ""
        raise SpecError(f"This is not valid YAML{where}: {getattr(exc, 'problem', exc)}") from None
    if not isinstance(raw, dict):
        raise SpecError("A test needs 'name', 'target' and 'steps'.")
    unknown = set(raw) - {"name", "target", "steps"}
    if unknown:
        raise SpecError(f"Unknown top-level setting: {', '.join(sorted(map(str, unknown)))}.")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SpecError("Give the test a 'name'.")
    target = parse_target(raw.get("target"))
    steps_raw = raw.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise SpecError("A test needs at least one step under 'steps'.")
    steps = [_parse_step(step, target, index) for index, step in enumerate(steps_raw, 1)]
    return Spec(name.strip(), target, steps)


_PLACEHOLDER = re.compile(r"\[[A-Z][A-Z _]{3,}\]|<[A-Z][A-Z _]{3,}>|\bTODO\b|\bFIXME\b")


def placeholders(spec: Spec) -> List[str]:
    """Unfinished values such as ``[EXPECTED TEXT]`` that would make a test meaningless."""
    found: List[str] = []
    for step in spec.steps:
        for match in _PLACEHOLDER.findall(str(step.value)):
            if match not in found:
                found.append(match)
    return found
