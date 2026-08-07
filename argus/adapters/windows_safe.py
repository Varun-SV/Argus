"""Safe Windows UIA adapter.

This adapter deliberately avoids host-wide mouse and keyboard injection. It
operates only on UI Automation elements discovered from the application under
test. Applications that do not expose usable UIA patterns can opt into the
legacy Windows adapter explicitly through ``ARGUS_INPUT_MODE=physical``.
"""
from __future__ import annotations

import time

from argus.adapters.base import AdapterError
from argus.adapters.windows_gui import WindowsGUIAdapter


class SafeWindowsGUIAdapter(WindowsGUIAdapter):
    """Windows adapter restricted to semantic, target-owned UIA operations."""

    def validate_action(self, action: dict) -> None:
        kind = action.get("action", "")
        if kind in {"click", "double_click", "right_click"}:
            if "element_id" not in action:
                raise AdapterError(
                    "coordinate input is disabled in safe Windows mode; use an element_id"
                )
            self._element(action["element_id"])
        if kind in {"double_click", "right_click", "key", "scroll"}:
            raise AdapterError(
                f"'{kind}' requires host input and is disabled in safe Windows mode"
            )
        if kind == "type" and "element_id" not in action:
            raise AdapterError(
                "unfocused typing is disabled in safe Windows mode; use an element_id"
            )

    def act(self, action: dict) -> str:
        kind = (action.get("action") or "").lower()

        if kind == "click":
            element_id = action["element_id"]
            el = self._element(element_id)
            errors = []
            for method in ("invoke", "select", "toggle", "click"):
                fn = getattr(el, method, None)
                if not callable(fn):
                    continue
                try:
                    fn()
                    return f"semantic click on element {element_id}"
                except Exception as exc:
                    errors.append(f"{method}: {exc}")
            detail = "; ".join(errors[-2:]) if errors else "no supported UIA pattern"
            raise AdapterError(f"element has no usable semantic click action ({detail})")

        if kind == "type":
            element_id = action["element_id"]
            text = str(action.get("text", ""))
            el = self._element(element_id)
            errors = []
            for method in ("set_edit_text", "set_value"):
                fn = getattr(el, method, None)
                if not callable(fn):
                    continue
                try:
                    fn(text)
                    return f"semantically set element {element_id} to {text!r}"
                except Exception as exc:
                    errors.append(f"{method}: {exc}")
            detail = "; ".join(errors[-2:]) if errors else "no Value/Edit UIA pattern"
            raise AdapterError(f"element cannot accept semantic text input ({detail})")

        if kind == "wait":
            seconds = min(float(action.get("seconds", 1.0)), 30.0)
            time.sleep(seconds)
            return f"waited {seconds}s"

        if kind == "menu":
            path = str(action.get("path", ""))
            self._top_window().menu_select(path)
            return f"selected menu {path}"

        raise AdapterError(
            f"safe Windows mode cannot execute '{kind}' without host-wide input"
        )
