from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows UIA regression test")


class _Point(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


def _cursor_position() -> tuple[int, int]:
    point = _Point()
    assert ctypes.windll.user32.GetCursorPos(ctypes.byref(point))
    return point.x, point.y


def _foreground_window() -> int:
    return int(ctypes.windll.user32.GetForegroundWindow())


def test_semantic_actions_do_not_move_cursor_or_steal_foreground():
    from pywinauto import Application

    from argus.adapters.windows_safe import SafeWindowsGUIAdapter

    fixture = Path(__file__).parent / "fixtures" / "windows_uia_target.py"
    target = SafeWindowsGUIAdapter()
    unrelated_proc = None

    try:
        target.launch(f'{sys.executable} {fixture} "Argus UIA Safety Target"')
        obs = target.observe(include_screenshot=False)
        edit_id = next(el.element_id for el in obs.elements if el.control_type == "Edit")
        button_id = next(
            el.element_id
            for el in obs.elements
            if el.control_type == "Button" and el.name == "Apply"
        )

        unrelated_proc = subprocess.Popen(
            [sys.executable, str(fixture), "Argus Unrelated Foreground"]
        )
        unrelated = Application(backend="uia").connect(
            process=unrelated_proc.pid, timeout=10
        )
        unrelated_window = unrelated.top_window()
        unrelated_window.wait("visible", timeout=10)
        unrelated_window.set_focus()
        time.sleep(0.5)

        foreground_before = _foreground_window()
        cursor_before = _cursor_position()
        assert foreground_before == int(unrelated_window.handle)

        target.execute({"action": "type", "element_id": edit_id, "text": "hello"})
        time.sleep(0.2)
        assert _cursor_position() == cursor_before, "UIA Value.SetValue moved the physical cursor"
        assert _foreground_window() == foreground_before, "UIA Value.SetValue changed foreground focus"

        target.execute({"action": "click", "element_id": button_id})
        time.sleep(0.2)
        assert _cursor_position() == cursor_before, "UIA Invoke moved the physical cursor"
        assert _foreground_window() == foreground_before, "UIA Invoke changed foreground focus"

        assert target.observe(include_screenshot=False).find_text("hello")
    finally:
        try:
            target.close()
        except Exception:
            pass
        if unrelated_proc is not None and unrelated_proc.poll() is None:
            unrelated_proc.terminate()
            try:
                unrelated_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                unrelated_proc.kill()


def test_descendant_ui_outlives_launcher_and_close_cleans_verified_child():
    """A short-lived launcher must not become the target liveness authority."""
    import psutil

    from argus.adapters.windows_safe import SafeWindowsGUIAdapter

    launcher = Path(__file__).parent / "fixtures" / "windows_uia_launcher.py"
    target = SafeWindowsGUIAdapter()
    attached_pid = None
    attached_created = None

    try:
        target.launch(f'{sys.executable} {launcher} "Argus Descendant Lifecycle Target"')
        assert target._proc is not None
        launcher_pid = target._proc.pid
        attached_pid = target._attached_pid
        attached_created = target._attached_create_time

        assert attached_pid is not None
        assert attached_created is not None
        assert attached_pid != launcher_pid, "safe mode attached to the launcher instead of its UI child"

        # The bootstrapper exits, but the verified child UI remains the target.
        target._proc.wait(timeout=5)
        assert target._proc.poll() is not None

        obs = target.observe(include_screenshot=False)
        assert obs.process_alive is True
        assert "Argus Descendant Lifecycle Target" in obs.window_title
        assert any(el.control_type == "Edit" for el in obs.elements)

        target.close()

        # close() must clean up the pinned child identity even though the
        # original launcher Popen has already exited.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                proc = psutil.Process(attached_pid)
                same_identity = abs(proc.create_time() - attached_created) <= 0.01
                if not same_identity or not proc.is_running():
                    break
            except psutil.NoSuchProcess:
                break
            time.sleep(0.1)
        else:
            pytest.fail("verified descendant UI process was left running after target.close()")
    finally:
        try:
            target.close()
        except Exception:
            pass
        if attached_pid is not None and attached_created is not None:
            try:
                proc = psutil.Process(attached_pid)
                if abs(proc.create_time() - attached_created) <= 0.01 and proc.is_running():
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
