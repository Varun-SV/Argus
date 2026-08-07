"""Validation and normalization for actions emitted by LLM providers.

The model-facing protocol is intentionally JSON/dict based, but adapters must
never receive arbitrary model output directly. This module defines the
supported action vocabulary and validates executable fields before execution
reaches an adapter or platform API.

Keyboard actions deliberately use an Argus-owned canonical grammar. Model
output is never allowed to contain pywinauto, X11/xdotool, Playwright, or other
backend-specific key syntax; adapters translate the validated canonical chord
to their native representation at the final execution boundary.
"""
from __future__ import annotations

import re
from typing import Any, Dict


class ActionValidationError(ValueError):
    """Raised when an action is malformed or outside Argus' action schema."""


SUPPORTED_ACTIONS = {
    "click",
    "double_click",
    "right_click",
    "type",
    "key",
    "scroll",
    "menu",
    "wait",
    "done",
    "navigate",
    "run",
    "execute",
    "report_bug",
}

# Canonical key vocabulary. Intentionally excludes Windows/Super/Meta and any
# raw virtual-key/X11 spelling. Additions here must be translated by every
# adapter that advertises the ``key`` capability.
KEY_MODIFIERS = ("ctrl", "alt", "shift")
_KEY_MODIFIER_SET = set(KEY_MODIFIERS)
KEY_NAMED = {
    "enter",
    "tab",
    "esc",
    "space",
    "backspace",
    "delete",
    "up",
    "down",
    "left",
    "right",
    "home",
    "end",
    "pageup",
    "pagedown",
    "insert",
    "minus",
    "equals",
    "comma",
    "period",
    "slash",
    "semicolon",
    "quote",
    "backquote",
    "bracketleft",
    "bracketright",
    "backslash",
    *{f"f{i}" for i in range(1, 13)},
}
KEY_ALIASES = {
    "control": "ctrl",
    "return": "enter",
    "escape": "esc",
    "pgup": "pageup",
    "pgdn": "pagedown",
}
_KEY_TOKEN_RE = re.compile(r"^[a-z0-9]+$")


def canonicalize_key_chord(keys: str) -> str:
    """Validate and normalize an Argus key chord.

    Grammar: zero or more modifiers from ``ctrl|alt|shift`` plus exactly one
    key, separated by ``+``. The key must be a single ASCII letter/digit or a
    named key from :data:`KEY_NAMED`. Raw backend syntax such as ``{VK_LWIN}``,
    ``Super_L``, bracketed expressions, whitespace-delimited backend commands,
    and unknown tokens is rejected before any adapter can see it.
    """
    if not isinstance(keys, str) or not keys.strip():
        raise ActionValidationError("key requires a non-empty keys string")

    raw_parts = [part.strip().lower() for part in keys.split("+")]
    if not raw_parts or any(not part for part in raw_parts):
        raise ActionValidationError("key chord contains an empty token")
    if len(raw_parts) > len(KEY_MODIFIERS) + 1:
        raise ActionValidationError("key chord may contain at most three modifiers and one key")

    parts: list[str] = []
    for raw in raw_parts:
        if not _KEY_TOKEN_RE.fullmatch(raw):
            raise ActionValidationError(
                f"key token {raw!r} uses unsupported/raw backend syntax"
            )
        part = KEY_ALIASES.get(raw, raw)
        parts.append(part)

    if len(parts) != len(set(parts)):
        raise ActionValidationError("key chord contains duplicate tokens")

    modifiers = [part for part in parts if part in _KEY_MODIFIER_SET]
    keys_only = [part for part in parts if part not in _KEY_MODIFIER_SET]
    if len(keys_only) != 1:
        raise ActionValidationError("key chord must contain exactly one non-modifier key")

    key = keys_only[0]
    if not ((len(key) == 1 and key.isascii() and key.isalnum()) or key in KEY_NAMED):
        raise ActionValidationError(f"unsupported key token {key!r}")

    ordered_modifiers = [modifier for modifier in KEY_MODIFIERS if modifier in modifiers]
    return "+".join([*ordered_modifiers, key])


def validate_action(action: Dict[str, Any]) -> Dict[str, Any]:
    """Return a normalized copy of *action* or raise ActionValidationError.

    Extra explanatory/model metadata (for example ``why``) is intentionally
    preserved; only executable fields are normalized and constrained here.
    """
    if not isinstance(action, dict):
        raise ActionValidationError("action must be a JSON object")

    normalized = dict(action)
    kind = normalized.get("action")
    if not isinstance(kind, str) or not kind.strip():
        raise ActionValidationError("action field must be a non-empty string")
    kind = kind.strip().lower()
    normalized["action"] = kind
    if kind not in SUPPORTED_ACTIONS:
        raise ActionValidationError(
            f"unknown action '{kind}' — supported: {', '.join(sorted(SUPPORTED_ACTIONS))}"
        )

    if kind in {"click", "double_click", "right_click"}:
        has_element = "element_id" in normalized
        has_xy = "x" in normalized or "y" in normalized
        if not has_element and not has_xy:
            raise ActionValidationError(f"{kind} requires element_id or x/y coordinates")
        if has_element:
            try:
                normalized["element_id"] = int(normalized["element_id"])
            except (TypeError, ValueError) as exc:
                raise ActionValidationError("element_id must be an integer") from exc
        if has_xy:
            if "x" not in normalized or "y" not in normalized:
                raise ActionValidationError("coordinate actions require both x and y")
            try:
                normalized["x"] = int(normalized["x"])
                normalized["y"] = int(normalized["y"])
            except (TypeError, ValueError) as exc:
                raise ActionValidationError("x and y must be integers") from exc

    elif kind == "type":
        if "text" not in normalized:
            raise ActionValidationError("type requires text")
        normalized["text"] = str(normalized["text"])
        if "element_id" in normalized:
            try:
                normalized["element_id"] = int(normalized["element_id"])
            except (TypeError, ValueError) as exc:
                raise ActionValidationError("element_id must be an integer") from exc

    elif kind == "key":
        normalized["keys"] = canonicalize_key_chord(normalized.get("keys"))

    elif kind == "scroll":
        direction = str(normalized.get("direction", "down")).strip().lower()
        if direction not in {"up", "down"}:
            raise ActionValidationError("scroll direction must be 'up' or 'down'")
        try:
            amount = int(normalized.get("amount", 3))
        except (TypeError, ValueError) as exc:
            raise ActionValidationError("scroll amount must be an integer") from exc
        if amount < 1 or amount > 100:
            raise ActionValidationError("scroll amount must be between 1 and 100")
        normalized["direction"] = direction
        normalized["amount"] = amount

    elif kind == "wait":
        try:
            seconds = float(normalized.get("seconds", 1.0))
        except (TypeError, ValueError) as exc:
            raise ActionValidationError("wait seconds must be numeric") from exc
        if seconds < 0 or seconds > 30:
            raise ActionValidationError("wait seconds must be between 0 and 30")
        normalized["seconds"] = seconds

    elif kind == "menu":
        path = normalized.get("path")
        if not isinstance(path, str) or not path.strip():
            raise ActionValidationError("menu requires a non-empty path")
        normalized["path"] = path.strip()

    elif kind == "navigate":
        url = normalized.get("url")
        if not isinstance(url, str) or not url.strip():
            raise ActionValidationError("navigate requires a non-empty url")
        normalized["url"] = url.strip()

    elif kind in {"run", "execute"}:
        if "command" in normalized:
            normalized["command"] = str(normalized["command"])

    elif kind == "done" and "success" in normalized:
        if not isinstance(normalized["success"], bool):
            raise ActionValidationError("done.success must be true or false")

    return normalized
