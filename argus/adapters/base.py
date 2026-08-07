"""Adapter interface — how Argus connects to a *target* application.

An adapter knows how to:
  * **launch** the target,
  * **observe** it (screenshot + accessibility tree + window state), and
  * **act** on it (click, type, press keys, …).

Adapters created through :func:`create_adapter` are wrapped in
:class:`PolicyAdapter`. This keeps the current engine API compatible while
ensuring every model-generated action is schema-validated and policy-checked
before it reaches platform input APIs.
"""

from __future__ import annotations

import os
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
    dialogs: List[str] = field(default_factory=list)
    error: Optional[str] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None
    exit_code: Optional[int] = None
    url: Optional[str] = None
    action_capabilities: Optional[dict] = None

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

    def capabilities(self) -> dict:
        """Describe model-visible executable actions for this adapter.

        The base contract is intentionally minimal/fail-closed. Concrete
        adapters must opt into every interactive capability they support so a
        new adapter can never silently inherit mouse, keyboard, or menu powers
        that its ``act`` implementation does not actually provide.
        """
        return {
            "actions": {
                "wait": {},
                "done": {},
            },
            "notes": ["This adapter has not declared interactive capabilities."],
        }

    def execute(self, action: dict) -> str:
        """Validate, authorize and execute one model-generated action."""
        from argus.actions import ActionValidationError, validate_action
        from argus.policy import ActionPolicyError, enforce_action_policy

        try:
            normalized = validate_action(action)
            enforce_action_policy(normalized)
        except (ActionValidationError, ActionPolicyError) as exc:
            raise AdapterError(f"action blocked: {exc}") from exc

        self.validate_action(normalized)
        return self.act(normalized)

    def validate_action(self, action: dict) -> None:
        """Platform-specific policy hook invoked immediately before ``act``."""

    @abstractmethod
    def act(self, action: dict) -> str:
        """Execute an already validated action; returns a short note."""

    @abstractmethod
    def close(self) -> None:
        """Tear down the target application."""


class PolicyAdapter(Adapter):
    """Compatibility guard around an adapter used by the current engines.

    The runner and roam engine historically call ``adapter.act`` directly.
    Wrapping adapters here means those calls still pass through the inner
    adapter's :meth:`execute` safety boundary without a broad engine rewrite.
    """

    def __init__(self, inner: Adapter) -> None:
        self.inner = inner
        self.type_name = inner.type_name

    def launch(self, target: str) -> None:
        self.inner.launch(target)

    def observe(self, include_screenshot: bool = True) -> Observation:
        obs = self.inner.observe(include_screenshot=include_screenshot)
        obs.action_capabilities = self.capabilities()
        return obs

    def capabilities(self) -> dict:
        return self.inner.capabilities()

    def act(self, action: dict) -> str:
        return self.inner.execute(action)

    def validate_action(self, action: dict) -> None:
        self.inner.validate_action(action)

    def close(self) -> None:
        self.inner.close()

    def __getattr__(self, name):
        return getattr(self.inner, name)


def _guard(adapter: Adapter) -> Adapter:
    return adapter if isinstance(adapter, PolicyAdapter) else PolicyAdapter(adapter)


def create_adapter(adapter_type: str) -> Adapter:
    adapter_type = (adapter_type or "").lower().strip()
    if adapter_type in ("desktop-gui", "desktop", "gui"):
        if sys.platform == "win32":
            input_mode = (os.environ.get("ARGUS_INPUT_MODE") or "safe").lower().strip()
            if input_mode in {"physical", "legacy"}:
                from argus.adapters.windows_gui import WindowsGUIAdapter
                return _guard(WindowsGUIAdapter())
            if input_mode not in {"safe", "semantic"}:
                raise AdapterError(
                    "ARGUS_INPUT_MODE must be 'safe'/'semantic' or explicit 'physical'"
                )
            from argus.adapters.windows_safe import SafeWindowsGUIAdapter
            return _guard(SafeWindowsGUIAdapter())
        if sys.platform.startswith("linux"):
            from argus.adapters.linux_gui import LinuxGUIAdapter
            return _guard(LinuxGUIAdapter())
        raise AdapterError(
            "the desktop-gui adapter supports Windows and Linux "
            f"(this is {sys.platform}). macOS support is on the roadmap."
        )
    if adapter_type in ("cli", "terminal", "shell"):
        from argus.adapters.cli_adapter import CLIAdapter
        return _guard(CLIAdapter())
    if adapter_type in ("browser", "web", "playwright"):
        from argus.adapters.browser_adapter import BrowserAdapter
        return _guard(BrowserAdapter())
    if adapter_type in ("linux-gui", "linux_gui", "x11"):
        from argus.adapters.linux_gui import LinuxGUIAdapter
        return _guard(LinuxGUIAdapter())
    raise AdapterError(
        f"unknown adapter '{adapter_type}' — available: desktop-gui, cli, browser"
    )
