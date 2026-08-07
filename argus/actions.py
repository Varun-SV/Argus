"""Validation and normalization for actions emitted by LLM providers.

The model-facing protocol is intentionally JSON/dict based, but adapters must
never receive arbitrary model output directly.  This module defines the
supported action vocabulary and validates the fields required by each action
before execution reaches an adapter or platform API.
"""
from __future__ import annotations

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
        keys = normalized.get("keys")
        if not isinstance(keys, str) or not keys.strip():
            raise ActionValidationError("key requires a non-empty keys string")
        normalized["keys"] = keys.strip()

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
