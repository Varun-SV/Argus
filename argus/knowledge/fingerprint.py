"""State fingerprinting and shared helpers for knowledge backends."""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict

from argus.adapters.base import Observation


def fingerprint(obs: Observation) -> str:
    """Stable hash ID for a UI state — same content → same ID across sessions."""
    parts = [
        obs.window_title.strip(),
        ",".join(sorted(f"{e.control_type}:{e.name}" for e in obs.elements[:30])),
        "dead" if not obs.process_alive else "alive",
        "err" if obs.error else "ok",
        obs.url or "",
    ]
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]


def semantic_description(obs: Observation) -> str:
    """Human-readable text used for embedding generation."""
    el_names = [e.name for e in obs.elements[:20] if e.name.strip()]
    parts = [f"Window: {obs.window_title}."]
    if obs.url:
        parts.append(f" URL: {obs.url}.")
    if obs.dialogs:
        parts.append(f" Dialogs: {', '.join(obs.dialogs)}.")
    if obs.error:
        parts.append(f" Error: {obs.error}.")
    if el_names:
        parts.append(f" Elements: {', '.join(el_names[:15])}.")
    return "".join(parts)


def target_key(target: str) -> str:
    """Filesystem-safe key derived from a target name."""
    return re.sub(r"[^a-z0-9]+", "-", target.lower()).strip("-")[:40] or "target"


def summarize_action(action: Dict[str, Any]) -> str:
    """One-line summary of a JSON action for graph edge labels."""
    kind = action.get("action", "")
    if kind == "click":
        ref = action.get("element_id") or f"({action.get('x')},{action.get('y')})"
        return f"click {ref}"
    if kind == "type":
        return f"type '{str(action.get('text', ''))[:20]}'"
    if kind == "menu":
        return f"menu {action.get('path', '')}"
    if kind == "key":
        return f"key {action.get('keys', '')}"
    if kind == "scroll":
        return f"scroll {action.get('direction', '')}"
    return kind
