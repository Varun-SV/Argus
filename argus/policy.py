"""Global execution policy for model-generated actions.

Platform adapters may add tighter target-specific checks, but every action
passes through this policy first.  The default policy blocks system-level key
combinations that can escape the application under test or interfere with the
host desktop.
"""
from __future__ import annotations

from typing import Dict, Any, Set


class ActionPolicyError(RuntimeError):
    """Raised when a valid action is forbidden by Argus' execution policy."""


def _key_parts(keys: str) -> Set[str]:
    aliases = {
        "control": "ctrl",
        "windows": "win",
        "super": "win",
        "meta": "win",
        "escape": "esc",
    }
    parts = set()
    for part in keys.lower().replace(" ", "").split("+"):
        if part:
            parts.add(aliases.get(part, part))
    return parts


def enforce_action_policy(action: Dict[str, Any]) -> None:
    """Enforce host-wide safety rules shared by every adapter."""
    if action.get("action") != "key":
        return

    parts = _key_parts(str(action.get("keys", "")))

    # The Windows/Super key intentionally has no use inside the application
    # boundary: it opens shell surfaces or global OS shortcuts.
    if "win" in parts:
        raise ActionPolicyError("system Windows/Super-key shortcuts are blocked")

    blocked = (
        {"alt", "tab"},
        {"alt", "f4"},
        {"ctrl", "esc"},
        {"ctrl", "alt", "delete"},
    )
    for combo in blocked:
        if combo.issubset(parts):
            raise ActionPolicyError(
                f"system-level key combination '{action.get('keys')}' is blocked"
            )
