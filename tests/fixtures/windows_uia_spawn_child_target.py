"""Win32 target whose semantic button action spawns a foreground child process."""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path

import win32api
import win32con
import win32gui


def _clean_arg(value: str) -> str:
    return value.strip().strip('"')


TITLE = _clean_arg(sys.argv[1]) if len(sys.argv) > 1 else "Argus Spawn Child Target"
MARKER = Path(_clean_arg(sys.argv[2])) if len(sys.argv) > 2 else None
IS_CHILD = len(sys.argv) > 3 and _clean_arg(sys.argv[3]) == "--child"
CLASS_NAME = f"ArgusSpawnChildTarget_{win32api.GetCurrentProcessId()}"
BUTTON_ID = 2001


def _force_foreground(hwnd: int) -> bool:
    """Force this test window foreground and report whether it succeeded."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    foreground = int(user32.GetForegroundWindow())
    foreground_thread = int(user32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
    current_thread = int(kernel32.GetCurrentThreadId())
    attached = False
    try:
        if foreground_thread and foreground_thread != current_thread:
            attached = bool(user32.AttachThreadInput(current_thread, foreground_thread, True))
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        return int(user32.GetForegroundWindow()) == int(hwnd)
    finally:
        if attached:
            user32.AttachThreadInput(current_thread, foreground_thread, False)


def _wnd_proc(hwnd, msg, wparam, lparam):
    if msg == win32con.WM_COMMAND and win32api.LOWORD(wparam) == BUTTON_ID and not IS_CHILD:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), f"{TITLE} Child", str(MARKER), "--child"]
        )
        return 0
    if msg == win32con.WM_DESTROY:
        win32gui.PostQuitMessage(0)
        return 0
    return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)


instance = win32api.GetModuleHandle(None)
wc = win32gui.WNDCLASS()
wc.hInstance = instance
wc.lpszClassName = CLASS_NAME
wc.lpfnWndProc = _wnd_proc
wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
win32gui.RegisterClass(wc)

hwnd = win32gui.CreateWindow(
    CLASS_NAME,
    TITLE,
    win32con.WS_OVERLAPPEDWINDOW,
    160,
    160,
    420,
    200,
    0,
    0,
    instance,
    None,
)

if not IS_CHILD:
    win32gui.CreateWindow(
        "Button",
        "Spawn Child",
        win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.BS_PUSHBUTTON,
        24,
        50,
        130,
        32,
        hwnd,
        BUTTON_ID,
        instance,
        None,
    )
else:
    win32gui.CreateWindow(
        "Static",
        "child",
        win32con.WS_CHILD | win32con.WS_VISIBLE,
        24,
        50,
        130,
        24,
        hwnd,
        2002,
        instance,
        None,
    )

win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)
win32gui.UpdateWindow(hwnd)

if IS_CHILD and MARKER is not None:
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if _force_foreground(hwnd):
            MARKER.write_text(str(win32api.GetCurrentProcessId()), encoding="utf-8")
            break
        time.sleep(0.01)

win32gui.PumpMessages()
