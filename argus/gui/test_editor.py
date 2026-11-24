"""Translate the desktop test form into the engine's existing test format."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import yaml

from argus.engine.spec import SpecError, parse_spec

ASSERTIONS = {
    "cli": ("exit_code_is", "stdout_contains", "stderr_contains"),
    "browser": ("page_title_contains", "url_contains", "text_visible"),
    "desktop-gui": ("text_visible", "window_title_contains", "element_exists",
                    "process_running", "dialog_open"),
}
PHASES = ("setup", "steps", "teardown")
_PLACEHOLDER = re.compile(r"\[(?:EXPECTED|TODO|REPLACE|INSERT)(?:[^\]]*)\]", re.IGNORECASE)


def validate_target(adapter: str, target: str) -> None:
    if adapter not in ASSERTIONS or not target or "\x00" in target or len(target) > 4096:
        raise SpecError("Choose a test type and enter its launch target.")
    if adapter == "browser":
        url = urlsplit(target)
        if url.scheme not in {"http", "https", "file"} or (url.scheme != "file" and not url.hostname):
            raise SpecError("Enter a complete website URL starting with https:// or http://.")
        if url.username or url.password:
            raise SpecError("Use a website URL without embedded credentials.")


def editor_view(data: dict) -> dict:
    target = data.get("target") or {}
    rows = []
    for phase in PHASES:
        for step in data.get(phase) or ():
            if isinstance(step, dict):
                assertion, value = next(iter(step["assert"].items()))
                rows.append(dict(phase=phase, kind="assert", assertion=assertion, value=value))
            else:
                rows.append(dict(phase=phase, kind="instruction", value=step))
    return {
        "name": data.get("name") or "", "adapter": target.get("adapter") or "desktop-gui",
        "target": str(target.get("launch") or ""), "rows": rows,
        "retries": data.get("retries", 0), "continue_on_failure": bool(data.get("continue_on_failure")),
        "staging": data.get("staging") or [], "collect": data.get("collect") or [],
    }


def _expected(assertion: str, value):
    if assertion == "process_running":
        if not isinstance(value, bool) and value not in ("true", "false"):
            raise SpecError("Choose whether a process should be running.")
        return value is True or value == "true"
    if assertion == "exit_code_is":
        try:
            return int(str(value))
        except (TypeError, ValueError):
            raise SpecError("An exit code must be a whole number.") from None
    if assertion == "element_exists" and isinstance(value, dict):
        expected = {key: str(value.get(key) or "").strip() for key in ("name", "control_type")}
        expected = {key: text for key, text in expected.items() if text}
        if expected and not any(_PLACEHOLDER.search(text) for text in expected.values()):
            return expected
    elif isinstance(value, str) and value.strip() and not _PLACEHOLDER.search(value):
        return value
    raise SpecError("Enter a concrete expected value for each check.")


def serialize_test(values: dict, previous: dict) -> tuple[str, object]:
    name = str(values.get("name") or "").strip()
    if not name or len(name) > 200:
        raise SpecError("Enter a test name of up to 200 characters.")
    adapter = str(values.get("adapter") or "")
    target = str(values.get("target") or "").strip()
    validate_target(adapter, target)
    rows = values.get("rows") or []
    if not 1 <= len(rows) <= 200:
        raise SpecError("Add between 1 and 200 checks or instructions.")
    for phase in PHASES:
        previous.pop(phase, None)
    for row in rows:
        phase, value = row.get("phase"), row.get("value")
        if phase not in PHASES:
            raise SpecError("Choose a step phase.")
        if row.get("kind") == "assert":
            assertion = row.get("assertion")
            if assertion not in ASSERTIONS[adapter]:
                raise SpecError("Choose a check supported by the selected test type.")
            step = {"assert": {assertion: _expected(assertion, value)}}
        elif isinstance(value, str) and value.strip() and len(value) <= 4096:
            step = value.strip()
        else:
            raise SpecError("Enter the instruction for each action.")
        previous.setdefault(phase, []).append(step)

    try:
        retries = int(str(values.get("retries", 0)))
    except ValueError:
        raise SpecError("Enter a whole number for retries.") from None
    if not 0 <= retries <= 20:
        raise SpecError("Choose between 0 and 20 retries.")
    previous.update(name=name, retries=retries, continue_on_failure=values.get("continue_on_failure") is True)
    previous.setdefault("target", {}).update(adapter=adapter, launch=target)
    for key in ("staging", "collect"):
        if key in values:
            previous[key] = values[key]
    encoded = yaml.safe_dump(previous, sort_keys=False, allow_unicode=True)
    try:
        return encoded, parse_spec(encoded)
    except (TypeError, ValueError) as exc:
        raise SpecError(str(exc)) from None


def has_draft_placeholder(spec) -> bool:
    from argus.engine.spec import AssertStep

    return any(isinstance(step, AssertStep) and _PLACEHOLDER.search(str(step.expected))
               for step in spec.steps)
