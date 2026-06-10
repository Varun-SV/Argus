"""Shared fakes: a scripted LLM provider and an in-memory GUI adapter."""

from __future__ import annotations

import json
from typing import List, Optional

import pytest

from argus.adapters.base import Adapter, AdapterError, Observation, UIElement
from argus.providers.base import LLMProvider, LLMResponse
from argus.tokens import TokenTracker, Usage


class FakeProvider(LLMProvider):
    """Replays a scripted list of replies; counts fake tokens."""

    type_name = "fake"

    def __init__(self, replies: List[str], vision: bool = True):
        super().__init__(model="fake-model", tracker=TokenTracker())
        self.replies = list(replies)
        self.vision = vision
        self.calls: List[dict] = []

    def chat(self, system, user, images=None) -> LLMResponse:
        self.calls.append({"system": system, "user": user, "images": images})
        reply = self.replies.pop(0) if self.replies else json.dumps(
            {"action": "done", "success": True, "note": "out of scripted replies"}
        )
        usage = Usage(prompt_tokens=100, completion_tokens=20)
        self._record(usage)
        return LLMResponse(text=reply, usage=usage)

    def _detect_vision(self) -> bool:
        return self.vision

    def check_connection(self) -> dict:
        return {"ok": True, "detail": "fake", "vision": self.vision}


class FakeApp:
    """A tiny stateful 'application' the FakeAdapter drives."""

    def __init__(self):
        self.title = "Fake App"
        self.text_content = ""
        self.menu_open = False
        self.dialogs: List[str] = []
        self.alive = True
        self.action_log: List[str] = []


class FakeAdapter(Adapter):
    type_name = "fake"

    def __init__(self, app: Optional[FakeApp] = None,
                 crash_after: Optional[int] = None):
        self.app = app or FakeApp()
        self.crash_after = crash_after
        self._action_count = 0
        self.launched_with: Optional[str] = None

    def launch(self, target: str) -> None:
        self.launched_with = target

    def observe(self, include_screenshot: bool = True) -> Observation:
        if not self.app.alive:
            return Observation(window_title="(process exited)", process_alive=False,
                               error="target process exited with code 1")
        elements = [
            UIElement(0, "Window", self.app.title, (0, 0, 800, 600)),
            UIElement(1, "Edit", self.app.text_content or "editor", (10, 40, 790, 560), depth=1),
            UIElement(2, "MenuItem", "File", (0, 0, 40, 30), depth=1),
        ]
        if self.app.menu_open:
            elements.append(UIElement(3, "MenuItem", "Save", (0, 30, 80, 60), depth=2))
        if self.app.text_content:
            elements.append(
                UIElement(4, "Text", self.app.text_content, (10, 40, 790, 80), depth=2)
            )
        return Observation(
            window_title=self.app.title,
            elements=elements,
            screenshot_png=b"\x89PNG fake" if include_screenshot else None,
            process_alive=True,
            dialogs=list(self.app.dialogs),
        )

    def act(self, action: dict) -> str:
        self._action_count += 1
        if self.crash_after is not None and self._action_count > self.crash_after:
            self.app.alive = False
        kind = action.get("action")
        self.app.action_log.append(kind)
        if kind == "type":
            self.app.text_content = action.get("text", "")
            return f"typed {action.get('text')!r}"
        if kind in ("click", "double_click", "right_click"):
            if action.get("element_id") == 2:
                self.app.menu_open = True
            return f"{kind} on element {action.get('element_id')}"
        if kind == "key":
            return f"pressed {action.get('keys')}"
        if kind == "wait":
            return "waited"
        if kind == "menu":
            self.app.menu_open = True
            return f"selected menu {action.get('path')}"
        if kind == "scroll":
            return "scrolled"
        raise AdapterError(f"unknown action '{kind}'")

    def close(self) -> None:
        self.app.alive = False


@pytest.fixture
def fake_app():
    return FakeApp()
