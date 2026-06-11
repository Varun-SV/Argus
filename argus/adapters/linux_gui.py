"""Linux GUI adapter — drives X11 applications via python-xlib / xdotool.

Requires `pip install argus-app-testing[linux]` plus an X display (real or
Xvfb). Set DISPLAY before running, e.g. `Xvfb :99 -screen 0 1920x1080x24 &
DISPLAY=:99 argus roam "./myapp"`.
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import List, Optional

from argus.adapters.base import Adapter, AdapterError, Observation, UIElement


class LinuxGUIAdapter(Adapter):
    """Minimal Linux GUI adapter using xdotool + scrot for screenshots."""

    type_name = "linux-gui"

    def __init__(self, display: Optional[str] = None) -> None:
        self._display = display or os.environ.get("DISPLAY", ":0")
        self._pid: Optional[int] = None
        self._proc: Optional[subprocess.Popen] = None

    def launch(self, target: str) -> None:
        env = {**os.environ, "DISPLAY": self._display}
        try:
            self._proc = subprocess.Popen(
                target, shell=True, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            self._pid = self._proc.pid
        except Exception as exc:
            raise AdapterError(f"linux-gui launch failed: {exc}") from exc
        time.sleep(1.0)  # give the window time to appear

    def observe(self, include_screenshot: bool = True) -> Observation:
        alive = self._proc is None or self._proc.poll() is None
        title = self._get_active_window_title()
        elements = self._get_elements()
        screenshot = self._take_screenshot() if include_screenshot else None
        return Observation(
            window_title=title or "(unknown)",
            elements=elements,
            screenshot_png=screenshot,
            process_alive=alive,
        )

    def act(self, action: dict) -> str:
        kind = (action.get("action") or "").lower()
        env = {**os.environ, "DISPLAY": self._display}

        if kind == "click":
            x, y = action.get("x", 0), action.get("y", 0)
            subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"], env=env)
            return f"clicked ({x},{y})"

        if kind == "double_click":
            x, y = action.get("x", 0), action.get("y", 0)
            subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "--repeat", "2", "1"], env=env)
            return f"double-clicked ({x},{y})"

        if kind == "type":
            text = action.get("text", "")
            subprocess.run(["xdotool", "type", "--clearmodifiers", text], env=env)
            return f"typed {text!r}"

        if kind == "key":
            keys = action.get("keys", "")
            xkey = keys.replace("ctrl+", "ctrl+").replace("ctrl", "ctrl")
            subprocess.run(["xdotool", "key", xkey], env=env)
            return f"pressed {keys}"

        if kind == "scroll":
            direction = action.get("direction", "down")
            button = "5" if direction == "down" else "4"
            for _ in range(int(action.get("amount", 3))):
                subprocess.run(["xdotool", "click", button], env=env)
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
        # Basic element discovery — real impl would use AT-SPI
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
