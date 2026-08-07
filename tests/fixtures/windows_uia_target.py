"""Tiny native Win32 UI target for the hosted-runner UIA regression test."""
import sys

import win32api
import win32con
import win32gui

TITLE = sys.argv[1] if len(sys.argv) > 1 else "Argus UIA Safety Target"
CLASS_NAME = f"ArgusUIASafetyTarget_{win32api.GetCurrentProcessId()}"
EDIT_ID = 1001
BUTTON_ID = 1002

_edit_hwnd = None
_status_hwnd = None


def _wnd_proc(hwnd, msg, wparam, lparam):
    global _edit_hwnd, _status_hwnd
    if msg == win32con.WM_COMMAND and win32api.LOWORD(wparam) == BUTTON_ID:
        if _edit_hwnd and _status_hwnd:
            win32gui.SetWindowText(_status_hwnd, win32gui.GetWindowText(_edit_hwnd))
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
    100,
    100,
    420,
    220,
    0,
    0,
    instance,
    None,
)

_edit_hwnd = win32gui.CreateWindowEx(
    win32con.WS_EX_CLIENTEDGE,
    "Edit",
    "",
    win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.ES_AUTOHSCROLL,
    24,
    28,
    350,
    28,
    hwnd,
    EDIT_ID,
    instance,
    None,
)

win32gui.CreateWindow(
    "Button",
    "Apply",
    win32con.WS_CHILD | win32con.WS_VISIBLE | win32con.BS_PUSHBUTTON,
    24,
    72,
    100,
    30,
    hwnd,
    BUTTON_ID,
    instance,
    None,
)

_status_hwnd = win32gui.CreateWindow(
    "Static",
    "idle",
    win32con.WS_CHILD | win32con.WS_VISIBLE,
    24,
    118,
    350,
    24,
    hwnd,
    1003,
    instance,
    None,
)

win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)
win32gui.UpdateWindow(hwnd)
win32gui.PumpMessages()
