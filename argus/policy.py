"""Global execution policy for model-generated actions.

Platform adapters may add tighter target-specific checks, but every action
passes through this policy first. Keyboard syntax is already canonicalized by
``argus.actions``; this layer blocks canonical chords that can escape the
application under test or interfere with the host desktop.
"""
from __future__ import annotations

from typing import Any, Dict


class ActionPolicyError(RuntimeError):
    """Raised when a valid action is forbidden by Argus' execution policy."""


def _key_parts(keys: str) -> set[str]:
    """Return parts from an already validated canonical Argus key chord."""
    return {part for part in keys.split("+") if part}


def enforce_action_policy(action: Dict[str, Any]) -> None:
    """Enforce host-wide safety rules shared by every adapter."""
    if action.get("action") != "key":
        return

    parts = _key_parts(str(action.get("keys", "")))

    # System/window switching and security-shell combinations. Windows/Super
    # is absent entirely from the canonical grammar, so it can never reach
    # this layer or a backend-specific translator.
    blocked = (
        {"alt", "tab"},
        {"alt", "esc"},
        {"alt", "f4"},
        {"ctrl", "esc"},
        {"ctrl", "shift", "esc"},
        {"ctrl", "alt", "delete"},
    )
    for combo in blocked:
        if combo.issubset(parts):
            raise ActionPolicyError(
                f"system-level key combination '{action.get('keys')}' is blocked"
            )
