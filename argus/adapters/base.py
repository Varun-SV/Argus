"""Adapter interface — how Argus connects to a *target* application.

An adapter knows how to:
  * **launch** the target,
  * **observe** it (screenshot + accessibility tree + window state), and
  * **act** on it (click, type, press keys, …).

All externally supplied actions must enter through :meth:`Adapter.execute`.
That method validates the model action, applies Argus' global execution
policy, lets the platform adapter enforce target-specific restrictions, and
only then dispatches to :meth:`Adapter.act`.

Actions are plain dicts on the model/provider boundary:

    {"action": "click",        "element_id": 12}
    {"action": "click",        "x": 100, "y": 200}
    {"action": "double_click", "element_id": 3}
    {"action": "right_click",  "element_id": 3}
    {"action": "type",         "text": "hello", "element_id": 4}
    {"action": "key",          "keys": "ctrl+s"}
    {"action": "scroll",       "direction": "down", "amount": 3}
    {"action": "wait",         "seconds": 1.5}
    {"action": "menu",         "path": "File->Save"}
    {"action": "done",         "success": true, "note": "..."}
"""

from __future__ import annotations

import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


class AdapterError(RuntimeError):
    """Raised when the target cannot be launched / observed / driven."""


@dataclass
class UIElement:
    """One node of the accessibility (UIA) tree, with a stable id the LLM
    can reference in actions."""

    element_id: int
    control_type: str
    name: str
    rect: tuple  # (left, top, right, bottom)
    enabled: bool = True
    depth: int = 0

    def describe(self) -> str:
        name = self.name.strip() or "(unnamed)"
        flags = "" if self.enabled else " [disabled]"
        return (
            f"{'  ' * self.depth}[{self.element_id}] {self.control_type} "
            f'"{name}"{flags} @({self.rect[0]},{self.rect[1]},{self.rect[2]},{self.rect[3]})'
        )


@dataclass
class Observation:
    """One snapshot of the target application."""

    window_title: str
    elements: List[UIElement] = field(default_factory=list)
    screenshot_png: Optional[bytes] = None
    process_alive: bool = True
    dialogs: List[str] = field(default_factory=list)  # titles of extra/popup windows
    error: Optional[str] = None
    # CLI adapter fields
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    exit_code: Optional[int] = None
    # Browser adapter field
    url: Optional[str] = None

    def tree_text(self, max_elements: int = 120) -> str:
        lines = [el.describe() for el in self.elements[:max_elements]]
        if len(self.elements) > max_elements:
            lines.append(f"... ({len(self.elements) - max_elements} more elements truncated)")
        return "\n".join(lines) or "(no accessible elements found)"

    def find_text(self, needle: str) -> bool:
        needle = needle.lower()
        if needle in self.window_title.lower():
            return True
        if self.stdout and needle in self.stdout.lower():
            return True
        if self.stderr and needle in self.stderr.lower():
            return True
        return any(needle in el.name.lower() for el in self.elements)


class Adapter(ABC):
    """Abstract base for target adapters."""

    type_name: str = "base"

    @abstractmethod
    def launch(self, target: str) -> None:
        """Start (or attach to) the application under test."""

    @abstractmethod
    def observe(self, include_screenshot: bool = True) -> Observation:
        """Capture the current state of the target."""

    def execute(self, action: dict) -> str:
        """Validate, authorize and execute one model-generated action.

        Engine code should call this method instead of ``act`` so future
        execution environments (including Argus Capsules) inherit the same
        safety boundary automatically.
        """
        from argus.actions import ActionValidationError, validate_action
        from argus.policy import ActionPolicyError, enforce_action_policy

        try:
            normalized = validate_action(action)
            enforce_action_policy(normalized)
        except (ActionValidationError, ActionPolicyError) as exc:
            raise AdapterError(f"action blocked: {exc}") from exc

        # Platform adapters can enforce target/window/origin boundaries here.
        self.validate_action(normalized)
        return self.act(normalized)

    def validate_action(self, action: dict) -> None:
        """Platform-specific policy hook invoked immediately before ``act``.

        The default implementation permits the validated action. Adapters may
        raise :class:`AdapterError` to deny actions outside their target.
        """

    @abstractmethod
    def act(self, action: dict) -> str:
        """Execute an already validated action; returns a short note."""

    @abstractmethod
    def close(self) -> None:
        """Tear down the target application."""


def create_adapter(adapter_type: str) -> Adapter:
    adapter_type = (adapter_type or "").lower().strip()
    if adapter_type in ("desktop-gui", "desktop", "gui"):
        if sys.platform == "win32":
            from argus.adapters.windows_gui import WindowsGUIAdapter
            return WindowsGUIAdapter()
        if sys.platform.startswith("linux"):
            from argus.adapters.linux_gui import LinuxGUIAdapter
            return LinuxGUIAdapter()
        raise AdapterError(
            "the desktop-gui adapter supports Windows and Linux "
            f"(this is {sys.platform}). macOS support is on the roadmap."
        )
    if adapter_type in ("cli", "terminal", "shell"):
        from argus.adapters.cli_adapter import CLIAdapter
        return CLIAdapter()
    if adapter_type in ("browser", "web", "playwright"):
        from argus.adapters.browser_adapter import BrowserAdapter
        return BrowserAdapter()
    if adapter_type in ("linux-gui", "linux_gui", "x11"):
        from argus.adapters.linux_gui import LinuxGUIAdapter
        return LinuxGUIAdapter()
    raise AdapterError(
        f"unknown adapter '{adapter_type}' — available: desktop-gui, cli, browser"
    )
