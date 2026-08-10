"""Test spec — parse ``.argus/*.test.yaml`` files."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Union

import yaml

ASSERTION_KINDS = (
    "text_visible",
    "window_title_contains",
    "element_exists",
    "process_running",
    "dialog_open",
    "stdout_contains",
    "stderr_contains",
    "exit_code_is",
    "url_contains",
    "page_title_contains",
)


class SpecError(ValueError):
    """Raised for malformed test files."""


@dataclass
class NLStep:
    text: str
    kind: str = "step"


@dataclass
class AssertStep:
    assertion: str
    expected: Union[str, bool, dict]
    kind: str = "assert"

    def describe(self) -> str:
        if isinstance(self.expected, dict):
            inner = ", ".join(f"{k}: {v}" for k, v in self.expected.items())
            return f"{self.assertion}: {{{inner}}}"
        return f"{self.assertion}: {self.expected!r}"


@dataclass(frozen=True)
class StageFile:
    source: str
    destination: str
    sha256: str = ""


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
    staging: List[StageFile] = field(default_factory=list)
    collect: List[str] = field(default_factory=list)

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


def _parse_staging(raw) -> List[StageFile]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SpecError("staging must be a list")
    out: List[StageFile] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SpecError(f"staging[{index}] must be a mapping")
        unknown = sorted(set(item) - {"source", "destination", "sha256"})
        if unknown:
            raise SpecError(f"staging[{index}] has unknown field(s): {', '.join(unknown)}")
        source = str(item.get("source") or "").strip()
        destination = str(item.get("destination") or "").strip()
        digest = str(item.get("sha256") or "").strip().lower()
        if not source or not destination:
            raise SpecError(f"staging[{index}] requires source and destination")
        if digest and (len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)):
            raise SpecError(f"staging[{index}].sha256 must be a 64-character hex digest")
        out.append(StageFile(source=source, destination=destination, sha256=digest))
    return out


def _parse_collect(raw) -> List[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise SpecError("collect must be a list")
    out: List[str] = []
    for index, item in enumerate(raw):
        if not isinstance(item, str) or not item.strip():
            raise SpecError(f"collect[{index}] must be a non-empty path string")
        out.append(item.strip())
    return out


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
        staging=_parse_staging(data.get("staging")),
        collect=_parse_collect(data.get("collect")),
    )


def load_spec(path: Path) -> TestSpec:
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        text = path.read_text(encoding="cp1252", errors="replace")
    return parse_spec(text, path=path)


def discover_tests(project_dir: Path) -> List[Path]:
    argus_dir = project_dir / ".argus"
    if not argus_dir.is_dir():
        return []
    return sorted(argus_dir.glob("*.test.yaml"))
