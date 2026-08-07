"""Linux GUI adapter — drives X11 applications via python-xlib / xdotool.

Requires `pip install argus-app-testing[linux]` plus an X display (real or
Xvfb). When no DISPLAY is set, Argus auto-starts Xvfb on :99 if available.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import List, Optional

from argus.adapters.base import Adapter, AdapterError, Observation, UIElement

_XVFB_DISPLAY = ":99"


class LinuxGUIAdapter(Adapter):
    """Minimal Linux GUI adapter using xdotool + scrot for screenshots."""

    type_name = "linux-gui"

    def __init__(self, display: Optional[str] = None, auto_xvfb: bool = True) -> None:
        env_display = display or os.environ.get("DISPLAY", "")
        self._auto_xvfb = auto_xvfb and not env_display
        self._display = env_display or ":0"
        self._xvfb_proc: Optional[subprocess.Popen] = None
        self._pid: Optional[int] = None
        self._proc: Optional[subprocess.Popen] = None

    def capabilities(self) -> dict:
        return {
            "actions": {
                "click": {"element_id": "none", "coordinates": True},
                "double_click": {"element_id": "none", "coordinates": True},
                "type": {"element_id": "none"},
                "key": {},
                "scroll": {},
                "wait": {},
                "done": {},
            },
            "notes": [
                "Linux GUI currently has no accessibility element discovery; clicks require coordinates.",
                "Key actions use Argus canonical syntax such as ctrl+s; X11 key names are not accepted.",
            ],
        }

    def _start_xvfb(self) -> None:
        if not shutil.which("Xvfb"):
            return
        try:
            self._xvfb_proc = subprocess.Popen(
                ["Xvfb", _XVFB_DISPLAY, "-screen", "0", "1920x1080x24"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._display = _XVFB_DISPLAY
            time.sleep(0.5)
        except Exception:
            self._xvfb_proc = None

    def launch(self, target: str) -> None:
        if self._auto_xvfb:
            self._start_xvfb()
        env = {**os.environ, "DISPLAY": self._display}
        try:
            self._proc = subprocess.Popen(
                target, shell=True, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._pid = self._proc.pid
        except Exception as exc:
            raise AdapterError(f"linux-gui launch failed: {exc}") from exc
        time.sleep(1.0)

    def observe(self, include_screenshot: bool = True) -> Observation:
        alive = self._proc is None or self._proc.poll() is None
        title = self._get_active_window_title()
        elements = self._get_elements()
        screenshot = self._take_screenshot() if include_screenshot else None
        error: Optional[str] = None
        if alive and not title and include_screenshot and screenshot is None:
            error = "no window title and screenshot failed — app may be frozen"
        return Observation(
            window_title=title or "(unknown)",
            elements=elements,
            screenshot_png=screenshot,
            process_alive=alive,
            error=error,
        )

    def _xdo(self, args: list, env: dict, timeout: int = 10) -> None:
        """Run an xdotool command; raise AdapterError if it times out or fails."""
        try:
            subprocess.run(["xdotool"] + args, env=env, timeout=timeout,
                           capture_output=True, check=False)
        except subprocess.TimeoutExpired:
            raise AdapterError(f"xdotool timed out after {timeout}s: {args}")
        except FileNotFoundError:
            raise AdapterError("xdotool not found — install it with: apt install xdotool")

    def act(self, action: dict) -> str:
        kind = (action.get("action") or "").lower()
        env = {**os.environ, "DISPLAY": self._display}

        if kind == "click":
            x, y = action.get("x", 0), action.get("y", 0)
            self._xdo(["mousemove", str(x), str(y), "click", "1"], env)
            return f"clicked ({x},{y})"

        if kind == "double_click":
            x, y = action.get("x", 0), action.get("y", 0)
            self._xdo(["mousemove", str(x), str(y), "click", "--repeat", "2", "1"], env)
            return f"double-clicked ({x},{y})"

        if kind == "type":
            text = action.get("text", "")
            self._xdo(["type", "--clearmodifiers", text], env)
            return f"typed {text!r}"

        if kind == "key":
            keys = str(action.get("keys", ""))
            self._xdo(["key", _to_xdotool_key(keys)], env)
            return f"pressed {keys}"

        if kind == "scroll":
            direction = action.get("direction", "down")
            button = "5" if direction == "down" else "4"
            for _ in range(int(action.get("amount", 3))):
                self._xdo(["click", button], env)
            return f"scrolled {direction}"

        if kind == "wait":
            time.sleep(min(float(action.get("seconds", 1)), 30))
            return "waited"

        if kind == "done":
            return "done"

        raise AdapterError(f"linux-gui adapter: unknown action '{kind}'")

    def close(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._xvfb_proc and self._xvfb_proc.poll() is None:
            self._xvfb_proc.terminate()
            try:
                self._xvfb_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._xvfb_proc.kill()

    def _get_active_window_title(self) -> str:
        try:
            env = {**os.environ, "DISPLAY": self._display}
            result = subprocess.run(
                ["xdotool", "getactivewindow", "getwindowname"],
                capture_output=True, text=True, env=env, timeout=3,
            )
            return result.stdout.strip()
        except Exception:
            return ""

    def _get_elements(self) -> List[UIElement]:
        # Basic element discovery — real impl would use AT-SPI.
        return []

    def _take_screenshot(self) -> Optional[bytes]:
        try:
            env = {**os.environ, "DISPLAY": self._display}
            result = subprocess.run(
                ["scrot", "-", "--stdout"],
                capture_output=True, env=env, timeout=5,
            )
            if result.returncode == 0:
                return result.stdout
        except Exception:
            pass
        return None


def _to_xdotool_key(combo: str) -> str:
    """Translate a validated canonical Argus key chord to xdotool syntax."""
    modifiers = {"ctrl": "ctrl", "alt": "alt", "shift": "shift"}
    named = {
        "enter": "Return",
        "tab": "Tab",
        "esc": "Escape",
        "space": "space",
        "backspace": "BackSpace",
        "delete": "Delete",
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
        "home": "Home",
        "end": "End",
        "pageup": "Prior",
        "pagedown": "Next",
        "insert": "Insert",
        "minus": "minus",
        "equals": "equal",
        "comma": "comma",
        "period": "period",
        "slash": "slash",
        "semicolon": "semicolon",
        "quote": "apostrophe",
        "backquote": "grave",
        "bracketleft": "bracketleft",
        "bracketright": "bracketright",
        "backslash": "backslash",
        **{f"f{i}": f"F{i}" for i in range(1, 13)},
    }
    parts = combo.split("+")
    translated = [modifiers[part] for part in parts[:-1]]
    translated.append(named.get(parts[-1], parts[-1]))
    return "+".join(translated)
