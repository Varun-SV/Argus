"""Safe Windows UIA adapter.

This adapter deliberately avoids host-wide mouse and keyboard injection. It
operates only on UI Automation elements discovered from a process Argus can
prove belongs to the requested target. Applications that do not expose usable
UIA patterns can opt into the legacy Windows adapter explicitly through
``ARGUS_INPUT_MODE=physical``.
"""
from __future__ import annotations

import ctypes
import shlex
import subprocess
import sys
import time
from typing import Optional, Set

from argus.adapters.base import AdapterError
from argus.adapters.windows_gui import WindowsGUIAdapter, _require_pywinauto


class SafeWindowsGUIAdapter(WindowsGUIAdapter):
    """Windows adapter restricted to semantic, target-owned UIA operations."""

    def __init__(self) -> None:
        super().__init__()
        self._owned_pids: Set[int] = set()

    def capabilities(self) -> dict:
        return {
            "actions": {
                "click": {"element_id": "required", "coordinates": False},
                "type": {"element_id": "required"},
                "wait": {},
                "done": {},
            },
            "notes": [
                "Safe Windows mode only permits direct UIA patterns on target-owned elements.",
                "click and type require an element_id; coordinates and global input are unavailable.",
            ],
        }

    def launch(self, target: str) -> None:
        """Launch and attach only when target ownership can be proven.

        Unlike the legacy adapter, safe mode never scans unrelated desktop
        windows by title. No PID/process-tree ownership proof means no attach.
        """
        Application, _Desktop = _require_pywinauto()
        exe_name = shlex.split(target, posix=False)[0].lower().split("\\")[-1]

        # A singleton is an explicit attach request by executable identity.
        if exe_name in self._SINGLETONS:
            try:
                self._app = Application(backend="uia").connect(path=exe_name)
                win = self._top_window()
                self._owned_pids = {int(win.process_id())}
                return
            except Exception as exc:
                self._app = None
                raise AdapterError(
                    f"'{exe_name}' is a system singleton — could not attach safely: {exc}"
                ) from exc

        try:
            self._proc = subprocess.Popen(shlex.split(target, posix=False))
        except OSError as exc:
            raise AdapterError(f"could not launch '{target}': {exc}") from exc

        root_pid = self._proc.pid
        self._owned_pids = {root_pid}
        last_err: Optional[Exception] = None

        # First require a window owned by the exact process Argus launched.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                app = Application(backend="uia").connect(process=root_pid, timeout=1)
                self._app = app
                self._verify_owned_top_window({root_pid})
                return
            except Exception as exc:
                self._app = None
                last_err = exc
                time.sleep(0.5)

        # Some WinUI/UWP launchers create the real UI in descendant processes.
        # Descendants are accepted only when psutil proves the relationship.
        try:
            import psutil

            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try:
                    parent = psutil.Process(root_pid)
                    child_pids = {c.pid for c in parent.children(recursive=True)}
                except psutil.NoSuchProcess:
                    child_pids = set()
                self._owned_pids = {root_pid, *child_pids}

                for child_pid in child_pids:
                    try:
                        app = Application(backend="uia").connect(process=child_pid, timeout=1)
                        self._app = app
                        self._verify_owned_top_window(self._owned_pids)
                        return
                    except Exception as exc:
                        self._app = None
                        last_err = exc
                time.sleep(0.5)
        except ImportError as exc:
            last_err = exc

        # Fail closed. Safe mode must never fall back to title matching across
        # the user's desktop because a same-title window may belong to them.
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._app = None
        self._owned_pids.clear()
        raise AdapterError(
            f"launched '{target}' (pid {root_pid}) but could not verify a target-owned "
            f"window within 15s; refusing title-based desktop fallback. Last error: {last_err}"
        )

    def _verify_owned_top_window(self, allowed_pids: Set[int]) -> None:
        win = self._top_window()
        pid = int(win.process_id())
        if pid not in allowed_pids:
            raise AdapterError(
                f"refusing window owned by pid {pid}; verified target pids are "
                f"{sorted(allowed_pids)}"
            )

    def validate_action(self, action: dict) -> None:
        kind = action.get("action", "")
        if kind in {"click", "double_click", "right_click"}:
            if "element_id" not in action:
                raise AdapterError(
                    "coordinate input is disabled in safe Windows mode; use an element_id"
                )
            self._element(action["element_id"])
        if kind in {"double_click", "right_click", "key", "scroll", "menu"}:
            raise AdapterError(
                f"'{kind}' is not guaranteed semantic and is disabled in safe Windows mode"
            )
        if kind == "type" and "element_id" not in action:
            raise AdapterError(
                "unfocused typing is disabled in safe Windows mode; use an element_id"
            )

    @staticmethod
    def _interface(element, name: str):
        """Return a UIA pattern interface without invoking wrapper conveniences."""
        try:
            return getattr(element, name)
        except Exception:
            return None

    @staticmethod
    def _foreground_window() -> int:
        if sys.platform != "win32":
            return 0
        return int(ctypes.windll.user32.GetForegroundWindow())

    @staticmethod
    def _window_pid(hwnd: int) -> int:
        if sys.platform != "win32" or not hwnd:
            return 0
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def _preserve_foreground(self, previous_hwnd: int) -> None:
        """Undo target-caused activation without fighting a user's new choice.

        Some UIA providers activate their application even for direct
        ``Value.SetValue``/``Invoke`` calls. We remember the user's foreground
        window before the action and, for a short settling window, restore it
        only when the foreground was stolen by a verified target process.
        If the user switched to any third-party window in the meantime, Argus
        immediately stops interfering and leaves that window alone.
        """
        if sys.platform != "win32" or not previous_hwnd:
            return
        if self._window_pid(previous_hwnd) in self._owned_pids:
            return

        user32 = ctypes.windll.user32
        deadline = time.monotonic() + 0.15
        saw_target_activation = False

        while time.monotonic() < deadline:
            current = self._foreground_window()
            if current and current != previous_hwnd:
                current_pid = self._window_pid(current)
                if current_pid not in self._owned_pids:
                    # The user (or another unrelated app) selected a different
                    # foreground window; never overwrite that newer choice.
                    return
                saw_target_activation = True
                if not user32.IsWindow(previous_hwnd):
                    raise AdapterError(
                        "target stole foreground focus and the previous user window no longer exists"
                    )
                user32.SetForegroundWindow(previous_hwnd)
            time.sleep(0.01)

        current = self._foreground_window()
        if current != previous_hwnd and self._window_pid(current) in self._owned_pids:
            raise AdapterError(
                "target stole foreground focus and Argus could not restore the user's window"
            )
        if saw_target_activation and current == previous_hwnd:
            return

    def act(self, action: dict) -> str:
        kind = (action.get("action") or "").lower()

        if kind == "click":
            element_id = action["element_id"]
            el = self._element(element_id)
            patterns = (
                ("iface_invoke", "Invoke"),
                ("iface_selection_item", "Select"),
                ("iface_toggle", "Toggle"),
            )
            errors = []
            for interface_name, method_name in patterns:
                interface = self._interface(el, interface_name)
                if interface is None:
                    continue
                method = getattr(interface, method_name, None)
                if not callable(method):
                    continue
                previous_hwnd = self._foreground_window()
                try:
                    method()
                    self._preserve_foreground(previous_hwnd)
                    return f"semantic click on element {element_id} via {method_name}"
                except AdapterError:
                    raise
                except Exception as exc:
                    errors.append(f"{method_name}: {exc}")
            detail = "; ".join(errors[-2:]) if errors else "no supported UIA pattern"
            raise AdapterError(f"element has no usable direct UIA click pattern ({detail})")

        if kind == "type":
            element_id = action["element_id"]
            text = str(action.get("text", ""))
            el = self._element(element_id)
            value = self._interface(el, "iface_value")
            set_value = getattr(value, "SetValue", None) if value is not None else None
            if not callable(set_value):
                raise AdapterError("element does not expose the UIA Value pattern")
            previous_hwnd = self._foreground_window()
            try:
                set_value(text)
                self._preserve_foreground(previous_hwnd)
            except AdapterError:
                raise
            except Exception as exc:
                raise AdapterError(f"UIA Value.SetValue failed: {exc}") from exc
            return f"semantically set element {element_id} to {text!r} via Value.SetValue"

        if kind == "wait":
            seconds = min(float(action.get("seconds", 1.0)), 30.0)
            time.sleep(seconds)
            return f"waited {seconds}s"

        raise AdapterError(
            f"safe Windows mode cannot execute '{kind}' without host-wide input"
        )
