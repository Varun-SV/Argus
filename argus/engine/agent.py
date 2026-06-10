"""The agent loop — one LLM-driven action cycle, shared by the test runner
(resolving natural-language steps) and the free-roam explorer.

Protocol: the model receives the current observation (window title, the
UIA accessibility tree with element ids, and — when the model is
multimodal — a screenshot) and must reply with **one JSON action**:

    {"action": "click", "element_id": 12, "why": "open the File menu"}
    {"action": "type", "text": "hello", "element_id": 4, "why": "..."}
    {"action": "done", "success": true, "note": "step complete"}

If the model is text-only, Argus warns once and proceeds with the
accessibility tree alone (no screenshots are sent).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Optional

from argus.adapters.base import Adapter, AdapterError, Observation
from argus.providers.base import LLMProvider, ProviderError

ACTION_SCHEMA = """\
Reply with EXACTLY ONE JSON object (no markdown fence, no prose) choosing one action:
  {"action":"click","element_id":<id>,"why":"..."}
  {"action":"double_click","element_id":<id>,"why":"..."}
  {"action":"right_click","element_id":<id>,"why":"..."}
  {"action":"click","x":<px>,"y":<px>,"why":"..."}
  {"action":"type","text":"...","element_id":<id optional>,"why":"..."}
  {"action":"key","keys":"ctrl+s","why":"..."}
  {"action":"scroll","direction":"down","amount":3,"why":"..."}
  {"action":"menu","path":"File->Save","why":"..."}
  {"action":"wait","seconds":1,"why":"..."}
  {"action":"done","success":true|false,"note":"..."}
Use element_id values from the UI TREE below. Prefer element ids over coordinates."""


class AgentParseError(RuntimeError):
    """The model reply contained no usable JSON action."""


@dataclass
class AgentTurn:
    action: dict
    raw_reply: str
    note: str = ""          # adapter's note of what was actually done
    error: Optional[str] = None


def extract_action(reply: str) -> dict:
    """Pull the first JSON object out of a model reply (tolerates fences/prose)."""
    text = reply.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1)
    start = text.find("{")
    if start == -1:
        raise AgentParseError(f"no JSON object in model reply: {reply[:200]!r}")
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                candidate = text[start : i + 1]
                try:
                    action = json.loads(candidate)
                except json.JSONDecodeError as exc:
                    raise AgentParseError(f"invalid JSON action: {exc}") from exc
                if not isinstance(action, dict) or "action" not in action:
                    raise AgentParseError(f"JSON has no 'action' field: {candidate[:200]}")
                return action
    raise AgentParseError(f"unbalanced JSON in model reply: {reply[:200]!r}")


def observation_prompt(obs: Observation, goal: str, history: list) -> str:
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
    parts.append(ACTION_SCHEMA)
    return "\n".join(parts)


def run_turn(
    provider: LLMProvider,
    adapter: Adapter,
    system_prompt: str,
    goal: str,
    history: list,
    use_vision: bool,
) -> AgentTurn:
    """One observe → think → act cycle."""
    obs = adapter.observe(include_screenshot=use_vision)
    if not obs.process_alive:
        return AgentTurn(
            action={"action": "done", "success": False, "note": obs.error or "target exited"},
            raw_reply="",
            error=obs.error or "target process exited",
        )

    images = [obs.screenshot_png] if (use_vision and obs.screenshot_png) else None
    try:
        response = provider.chat(
            system=system_prompt,
            user=observation_prompt(obs, goal, history),
            images=images,
        )
    except ProviderError as exc:
        return AgentTurn(action={"action": "done", "success": False}, raw_reply="", error=str(exc))

    try:
        action = extract_action(response.text)
    except AgentParseError as exc:
        return AgentTurn(action={"action": "done", "success": False}, raw_reply=response.text, error=str(exc))

    if action.get("action") == "done":
        return AgentTurn(action=action, raw_reply=response.text)

    try:
        note = adapter.act(action)
    except AdapterError as exc:
        return AgentTurn(action=action, raw_reply=response.text, error=str(exc))
    return AgentTurn(action=action, raw_reply=response.text, note=note)
