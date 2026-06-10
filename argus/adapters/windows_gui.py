"""Windows desktop-GUI adapter.

Observation:
  * **UIA accessibility tree** via pywinauto's ``uia`` backend — every
    visible control gets a stable ``element_id`` the LLM can act on.
  * **Screenshots** of the target window (PIL), sent to multimodal models.
  * **Dialog detection** — extra top-level windows of the same process
    (message boxes, error dialogs) are surfaced to the engine and the
    roam bug detector.

Action synthesis goes through pywinauto (mouse/keyboard + element methods).

This module imports pywinauto lazily so the rest of Argus works on any OS;
install the extra on Windows:  ``pip install argus-app-testing[windows]``.
"""

from __future__ import annotations

import shlex
import subprocess
import time
from typing import List, Optional

from argus.adapters.base import Adapter, AdapterError, Observation, UIElement

_MAX_ELEMENTS = 250
_MAX_DEPTH = 12


def _require_pywinauto():
    try:
        from pywinauto import Application, Desktop  # noqa: F401

        return Application, Desktop
    except ImportError as exc:
        raise AdapterError(
            "pywinauto is required for desktop-gui testing on Windows — "
            "install with: pip install argus-app-testing[windows]"
        ) from exc


class WindowsGUIAdapter(Adapter):
    type_name = "desktop-gui"

    def __init__(self) -> None:
        self._app = None  # pywinauto Application
        self._proc: Optional[subprocess.Popen] = None
        self._elements: List = []  # wrapper objects, index == element_id

    # ---- lifecycle -----------------------------------------------------

    def launch(self, target: str) -> None:
        Application, _ = _require_pywinauto()
        try:
            self._proc = subprocess.Popen(shlex.split(target, posix=False))
        except OSError as exc:
            raise AdapterError(f"could not launch '{target}': {exc}") from exc

        # Give the process a moment to create its window, then attach.
        deadline = time.monotonic() + 15
        last_err: Optional[Exception] = None
        while time.monotonic() < deadline:
            try:
                self._app = Application(backend="uia").connect(
                    process=self._proc.pid, timeout=1
                )
                self._top_window()  # raises if no window yet
                return
            except Exception as exc:  # pywinauto raises various types here
                last_err = exc
                time.sleep(0.5)
        raise AdapterError(
            f"launched '{target}' (pid {self._proc.pid}) but no window appeared "
            f"within 15s: {last_err}"
        )

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                if self._app is not None:
                    self._top_window().close()
                    time.sleep(1.0)
            except Exception:
                pass
            if self._proc.poll() is None:
                self._proc.kill()
        self._app = None
        self._proc = None
        self._elements = []

    # ---- observation ------------------------------------------------------

    def _top_window(self):
        if self._app is None:
            raise AdapterError("no application attached — call launch() first")
        win = self._app.top_window()
        win.wait("exists", timeout=5)
        return win

    def observe(self, include_screenshot: bool = True) -> Observation:
        if self._proc is not None and self._proc.poll() is not None:
            return Observation(
                window_title="(process exited)",
                process_alive=False,
                error=f"target process exited with code {self._proc.returncode}",
            )
        try:
            win = self._top_window()
            title = win.window_text()
        except Exception as exc:
            return Observation(
                window_title="(no window)",
                process_alive=self._proc is not None and self._proc.poll() is None,
                error=f"could not access target window: {exc}",
            )

        elements: List[UIElement] = []
        self._elements = []
        try:
            self._walk(win, elements, depth=0)
        except Exception as exc:
            return Observation(window_title=title, error=f"UIA tree walk failed: {exc}")

        screenshot: Optional[bytes] = None
        if include_screenshot:
            try:
                import io

                img = win.capture_as_image()
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                screenshot = buf.getvalue()
            except Exception:
                screenshot = None  # screenshot is best-effort

        return Observation(
            window_title=title,
            elements=elements,
            screenshot_png=screenshot,
            process_alive=True,
            dialogs=self._extra_windows(title),
        )

    def _walk(self, wrapper, out: List[UIElement], depth: int) -> None:
        if len(out) >= _MAX_ELEMENTS or depth > _MAX_DEPTH:
            return
        try:
            info = wrapper.element_info
            rect = info.rectangle
            element = UIElement(
                element_id=len(self._elements),
                control_type=info.control_type or "Unknown",
                name=(info.name or "")[:120],
                rect=(rect.left, rect.top, rect.right, rect.bottom),
                enabled=bool(getattr(info, "enabled", True)),
                depth=depth,
            )
            out.append(element)
            self._elements.append(wrapper)
        except Exception:
            return
        try:
            children = wrapper.children()
        except Exception:
            return
        for child in children:
            self._walk(child, out, depth + 1)

    def _extra_windows(self, main_title: str) -> List[str]:
        """Titles of other top-level windows of this process (dialogs)."""
        try:
            titles = []
            for w in self._app.windows():
                text = w.window_text()
                if text and text != main_title:
                    titles.append(text)
            return titles
        except Exception:
            return []

    # ---- actions ----------------------------------------------------------

    def _element(self, element_id) -> object:
        try:
            return self._elements[int(element_id)]
        except (IndexError, ValueError, TypeError) as exc:
            raise AdapterError(
                f"unknown element_id {element_id!r} — re-observe and use a listed id"
            ) from exc

    def act(self, action: dict) -> str:
        kind = (action.get("action") or "").lower()
        try:
            return self._dispatch(kind, action)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"action '{kind}' failed: {exc}") from exc

    def _dispatch(self, kind: str, action: dict) -> str:
        from pywinauto import mouse
        from pywinauto.keyboard import send_keys

        if kind == "click" or kind == "double_click" or kind == "right_click":
            if "element_id" in action:
                el = self._element(action["element_id"])
                if kind == "click":
                    el.click_input()
                elif kind == "double_click":
                    el.double_click_input()
                else:
                    el.right_click_input()
                return f"{kind} on element {action['element_id']}"
            x, y = int(action["x"]), int(action["y"])
            button = "right" if kind == "right_click" else "left"
            if kind == "double_click":
                mouse.double_click(coords=(x, y))
            else:
                mouse.click(button=button, coords=(x, y))
            return f"{kind} at ({x},{y})"

        if kind == "type":
            text = str(action.get("text", ""))
            if "element_id" in action:
                el = self._element(action["element_id"])
                el.click_input()
            # escape pywinauto's special chars, keep literal text literal
            send_keys(
                text.replace("{", "{{}").replace("}", "{}}")
                .replace("+", "{+}").replace("^", "{^}")
                .replace("%", "{%}").replace("~", "{~}")
                .replace("(", "{(}").replace(")", "{)}"),
                with_spaces=True,
            )
            return f"typed {text!r}"

        if kind == "key":
            keys = str(action.get("keys", ""))
            send_keys(_to_send_keys(keys))
            return f"pressed {keys}"

        if kind == "scroll":
            direction = action.get("direction", "down")
            amount = int(action.get("amount", 3))
            win = self._top_window()
            rect = win.rectangle()
            cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
            wheel = -amount if direction == "down" else amount
            mouse.scroll(coords=(cx, cy), wheel_dist=wheel)
            return f"scrolled {direction} x{amount}"

        if kind == "wait":
            seconds = min(float(action.get("seconds", 1.0)), 30.0)
            time.sleep(seconds)
            return f"waited {seconds}s"

        if kind == "menu":
            path = str(action.get("path", ""))
            win = self._top_window()
            win.menu_select(path)
            return f"selected menu {path}"

        raise AdapterError(
            f"unknown action '{kind}' — valid: click, double_click, right_click, "
            "type, key, scroll, wait, menu, done"
        )


def _to_send_keys(combo: str) -> str:
    """Translate 'ctrl+shift+s' style combos into pywinauto send_keys syntax."""
    mods = {"ctrl": "^", "control": "^", "alt": "%", "shift": "+", "win": "{VK_LWIN}"}
    named = {
        "enter": "{ENTER}", "return": "{ENTER}", "tab": "{TAB}", "esc": "{ESC}",
        "escape": "{ESC}", "space": " ", "backspace": "{BACKSPACE}", "delete": "{DELETE}",
        "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
        "home": "{HOME}", "end": "{END}", "pageup": "{PGUP}", "pagedown": "{PGDN}",
        **{f"f{i}": f"{{F{i}}}" for i in range(1, 13)},
    }
    parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
    prefix, keys = "", []
    for part in parts:
        if part in mods:
            prefix += mods[part]
        else:
            keys.append(named.get(part, part))
    return prefix + ("".join(keys) or "")
