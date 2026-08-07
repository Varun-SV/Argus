"""The agent loop — one LLM-driven action cycle, shared by the test runner
(resolving natural-language steps) and the free-roam explorer.

The model receives the current observation plus an adapter-specific action
schema. Adapters therefore never advertise actions they are guaranteed to
reject (for example global keyboard input in safe Windows mode).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import List, Optional

from argus.adapters.base import Adapter, AdapterError, Observation
from argus.providers.base import LLMProvider, ProviderError


_DEFAULT_CAPABILITIES = {
    "actions": {
        "click": {"element_id": "optional", "coordinates": True},
        "double_click": {"element_id": "optional", "coordinates": True},
        "right_click": {"element_id": "optional", "coordinates": True},
        "type": {"element_id": "optional"},
        "key": {},
        "scroll": {},
        "menu": {},
        "wait": {},
        "done": {},
    },
    "notes": ["Prefer element_id values from the UI tree over coordinates."],
}


def build_action_schema(
    capabilities: Optional[dict] = None,
    *,
    include_done: bool = True,
    include_report_bug: bool = False,
) -> str:
    """Render a strict model-facing schema from adapter capabilities."""
    caps = capabilities or _DEFAULT_CAPABILITIES
    actions = caps.get("actions", {})
    lines = [
        "Reply with EXACTLY ONE JSON object (no markdown fence, no prose) choosing one action:"
    ]

    for kind in actions:
        if kind == "done" and not include_done:
            continue
        spec = actions.get(kind) or {}
        if kind in {"click", "double_click", "right_click"}:
            element_mode = spec.get("element_id", "optional")
            if element_mode in {"required", "optional"}:
                lines.append(
                    f'  {{"action":"{kind}","element_id":<id>,"why":"..."}}'
                )
            if spec.get("coordinates"):
                lines.append(
                    f'  {{"action":"{kind}","x":<px>,"y":<px>,"why":"..."}}'
                )
        elif kind == "type":
            if spec.get("element_id") == "required":
                lines.append(
                    '  {"action":"type","text":"...","element_id":<id>,"why":"..."}'
                )
            else:
                lines.append(
                    '  {"action":"type","text":"...","element_id":<id optional>,"why":"..."}'
                )
        elif kind == "key":
            lines.append('  {"action":"key","keys":"ctrl+s","why":"..."}')
        elif kind == "scroll":
            lines.append(
                '  {"action":"scroll","direction":"down","amount":3,"why":"..."}'
            )
        elif kind == "menu":
            lines.append('  {"action":"menu","path":"File->Save","why":"..."}')
        elif kind == "navigate":
            lines.append('  {"action":"navigate","url":"https://...","why":"..."}')
        elif kind in {"run", "execute"}:
            lines.append(
                f'  {{"action":"{kind}","command":"...","why":"..."}}'
            )
        elif kind == "wait":
            lines.append('  {"action":"wait","seconds":1,"why":"..."}')
        elif kind == "done":
            lines.append('  {"action":"done","success":true|false,"note":"..."}')

    if include_report_bug:
        lines.append(
            '  {"action":"report_bug","title":"...","severity":"low|medium|high",'
            '"expected":"...","actual":"...","why":"..."}'
        )

    for note in caps.get("notes", []):
        lines.append(str(note))
    return "\n".join(lines)


# Backwards-compatible generic schema for callers that do not yet have an adapter.
ACTION_SCHEMA = build_action_schema()

BATCH_ADDENDUM = """\

PROGRESSIVE MODE: You have visited this state {count} times before.
You may return a JSON ARRAY of up to {max_batch} actions to execute in sequence
if you are highly confident about all of them. Every action in the array MUST
match one of the allowed actions above. Return a single action dict if uncertain."""


class AgentParseError(RuntimeError):
    """The model reply contained no usable JSON action."""


@dataclass
class AgentTurn:
    action: dict
    raw_reply: str
    note: str = ""
    error: Optional[str] = None
    state_id: str = ""


def extract_action(reply: str) -> dict:
    """Pull the first JSON object out of a model reply (single-action compat)."""
    actions = extract_actions(reply)
    return actions[0]


def extract_actions(reply: str) -> List[dict]:
    """Parse a model reply into a list of action dicts.

    Handles both a single JSON object and a JSON array of objects.
    """
    text = reply.strip()
    fenced = re.search(r"```(?:json)?\s*([\[{].*?[\]}])\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    if text.startswith("["):
        end = text.rfind("]")
        if end != -1:
            try:
                parsed = json.loads(text[: end + 1])
                if isinstance(parsed, list) and parsed:
                    result = []
                    for item in parsed:
                        if isinstance(item, dict) and "action" in item:
                            result.append(item)
                    if result:
                        return result
            except json.JSONDecodeError:
                pass

    start = text.find("{")
    if start == -1:
        raise AgentParseError(f"no JSON in model reply: {reply[:200]!r}")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start: i + 1]
                try:
                    action = json.loads(candidate)
                except json.JSONDecodeError as exc:
                    raise AgentParseError(f"invalid JSON action: {exc}") from exc
                if not isinstance(action, dict) or "action" not in action:
                    raise AgentParseError(f"JSON has no 'action' field: {candidate[:200]}")
                return [action]
    raise AgentParseError(f"unbalanced JSON in model reply: {reply[:200]!r}")


def observation_prompt(
    obs: Observation,
    goal: str,
    history: list,
    knowledge_context: Optional[str] = None,
    batch_hint: int = 1,
    action_schema: Optional[str] = None,
) -> str:
    parts = [f"GOAL: {goal}", ""]
    if history:
        parts.append("ACTIONS SO FAR:")
        parts.extend(f"  {i+1}. {h}" for i, h in enumerate(history[-8:]))
        parts.append("")
    parts.append(f"WINDOW TITLE: {obs.window_title}")
    if obs.dialogs:
        parts.append(f"OPEN DIALOGS/POPUPS: {', '.join(obs.dialogs)}")
    if obs.error:
        parts.append(f"OBSERVATION ERROR: {obs.error}")
    parts.append("")
    parts.append("UI TREE (element_id, type, name, rect):")
    parts.append(obs.tree_text())
    parts.append("")
    schema = action_schema or ACTION_SCHEMA
    if batch_hint >= 5:
        max_batch = min(batch_hint // 5, 5)
        schema += BATCH_ADDENDUM.format(count=batch_hint, max_batch=max_batch)
    parts.append(schema)
    if knowledge_context:
        parts.append("")
        parts.append("RELEVANT PAST EXPERIENCES:")
        parts.append(knowledge_context)
    return "\n".join(parts)


def run_turn(
    provider: LLMProvider,
    adapter: Adapter,
    system_prompt: str,
    goal: str,
    history: list,
    use_vision: bool,
    knowledge_store=None,
    target: str = "",
    session_id: str = "",
    prev_state_id: str = "",
) -> "AgentTurn":
    """One observe → think → act cycle (single action, for runner.py)."""
    obs = adapter.observe(include_screenshot=use_vision)
    if not obs.process_alive:
        return AgentTurn(
            action={"action": "done", "success": False, "note": obs.error or "target exited"},
            raw_reply="",
            error=obs.error or "target process exited",
        )

    state_id = ""
    knowledge_context: Optional[str] = None
    batch_hint = 1
    if knowledge_store is not None:
        try:
            state_id = knowledge_store.record_state(obs, target, session_id, len(history))
            ctx = knowledge_store.retrieve(obs, target)
            if not ctx.is_empty():
                knowledge_context = ctx.format()
            batch_hint = knowledge_store.confidence_for_state(state_id)
        except Exception:
            pass

    images = [obs.screenshot_png] if (use_vision and obs.screenshot_png) else None
    action_schema = build_action_schema(adapter.capabilities())
    try:
        response = provider.chat(
            system=system_prompt,
            user=observation_prompt(
                obs,
                goal,
                history,
                knowledge_context=knowledge_context,
                batch_hint=batch_hint,
                action_schema=action_schema,
            ),
            images=images,
        )
    except ProviderError as exc:
        return AgentTurn(
            action={"action": "done", "success": False}, raw_reply="", error=str(exc),
            state_id=state_id,
        )

    try:
        actions = extract_actions(response.text)
        action = actions[0]
    except AgentParseError as exc:
        return AgentTurn(
            action={"action": "done", "success": False}, raw_reply=response.text, error=str(exc),
            state_id=state_id,
        )

    if action.get("action") == "done":
        return AgentTurn(action=action, raw_reply=response.text, state_id=state_id)

    try:
        note = adapter.act(action)
    except AdapterError as exc:
        return AgentTurn(action=action, raw_reply=response.text, error=str(exc), state_id=state_id)
    return AgentTurn(action=action, raw_reply=response.text, note=note, state_id=state_id)
