"""Test spec — parse ``.argus/*.test.yaml`` files.

Hybrid-agentic format: natural-language steps (the LLM fills in the
ambiguity) interleaved with structured ``assert`` blocks (always
authoritative, executed deterministically — never by the model).

Supported assertions (desktop-gui):

    - assert:
        text_visible: "hello"               # any element/window text contains it
    - assert:
        window_title_contains: "Notepad"
    - assert:
        element_exists:
          name: "Save"                      # substring match on element name
          control_type: MenuItem            # optional
    - assert:
        process_running: true
    - assert:
        dialog_open: "Error"                # a popup window whose title contains
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

import yaml

ASSERTION_KINDS = (
    # desktop-gui
    "text_visible",
    "window_title_contains",
    "element_exists",
    "process_running",
    "dialog_open",
    # cli
    "stdout_contains",
    "stderr_contains",
    "exit_code_is",
    # browser
    "url_contains",
    "page_title_contains",
)


class SpecError(ValueError):
    """Raised for malformed test files."""


@dataclass
class NLStep:
    """A natural-language step, resolved by the LLM at run time."""

    text: str
    kind: str = "step"  # step | setup | teardown


@dataclass
class AssertStep:
    """A structured assertion, executed deterministically."""

    assertion: str
    expected: Union[str, bool, dict]
    kind: str = "assert"

    def describe(self) -> str:
        if isinstance(self.expected, dict):
            inner = ", ".join(f"{k}: {v}" for k, v in self.expected.items())
            return f"{self.assertion}: {{{inner}}}"
        return f"{self.assertion}: {self.expected!r}"


Step = Union[NLStep, AssertStep]


@dataclass
class TestSpec:
    name: str
    adapter: str
    launch: str
    steps: List[Step] = field(default_factory=list)
    path: Optional[Path] = None
    continue_on_failure: bool = False
    retries: int = 0

    @property
    def file_name(self) -> str:
        return self.path.name if self.path else self.name


def _parse_step(raw, kind: str) -> Step:
    if isinstance(raw, str):
        if kind == "teardown" and raw.strip().lower() == "close":
            return NLStep(text="close", kind="teardown")
        return NLStep(text=raw, kind=kind)
    if isinstance(raw, dict) and "assert" in raw:
        body = raw["assert"]
        if not isinstance(body, dict) or len(body) != 1:
            raise SpecError(
                f"assert block must contain exactly one assertion, got: {body!r}"
            )
        assertion, expected = next(iter(body.items()))
        if assertion not in ASSERTION_KINDS:
            raise SpecError(
                f"unknown assertion '{assertion}' — supported: {', '.join(ASSERTION_KINDS)}"
            )
        return AssertStep(assertion=assertion, expected=expected)
    raise SpecError(f"step must be a string or an assert block, got: {raw!r}")


def parse_spec(text: str, path: Optional[Path] = None) -> TestSpec:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SpecError(f"invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SpecError("test file must be a YAML mapping")

    target = data.get("target") or {}
    if not isinstance(target, dict) or not target.get("launch"):
        raise SpecError("test needs target: {adapter: ..., launch: ...}")

    steps: List[Step] = []
    for raw in data.get("setup") or []:
        steps.append(_parse_step(raw, "setup"))
    for raw in data.get("steps") or []:
        steps.append(_parse_step(raw, "step"))
    for raw in data.get("teardown") or []:
        steps.append(_parse_step(raw, "teardown"))
    if not steps:
        raise SpecError("test has no steps")

    return TestSpec(
        name=str(data.get("name") or (path.stem if path else "unnamed test")),
        adapter=str(target.get("adapter") or "desktop-gui"),
        launch=str(target["launch"]),
        steps=steps,
        path=path,
        continue_on_failure=bool(data.get("continue_on_failure", False)),
        retries=int(data.get("retries", 0)),
    )


def load_spec(path: Path) -> TestSpec:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="cp1252", errors="replace")
    return parse_spec(text, path=path)


def discover_tests(project_dir: Path) -> List[Path]:
    """All ``*.test.yaml`` files under ``.argus/`` (sorted)."""
    argus_dir = project_dir / ".argus"
    if not argus_dir.is_dir():
        return []
    return sorted(argus_dir.glob("*.test.yaml"))
