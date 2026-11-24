"""Readiness and on-demand setup for desktop test adapters."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

from argus.adapters.browser_adapter import chromium_launch_options


def browser_readiness() -> dict:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {"state": "unavailable", "detail": "This Argus build does not include Playwright."}
    try:
        with sync_playwright() as driver:
            executable = Path(driver.chromium.executable_path)
            options = chromium_launch_options(driver.chromium)
    except Exception:
        return {"state": "unavailable", "detail": "The browser driver could not start."}
    if executable.is_file():
        return {"state": "installed", "detail": "Chromium is installed."}
    if options:
        return {"state": "installed", "detail": "System Chromium is available. Check launch to verify it."}
    return {"state": "installable", "detail": "Download Chromium to run browser tests."}


def install_browser() -> dict:
    try:
        from playwright._impl._driver import compute_driver_executable, get_driver_env

        command = [*compute_driver_executable(), "install", "chromium"]
        result = subprocess.run(command, env=get_driver_env(), check=False, timeout=900,
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, ImportError, RuntimeError, subprocess.TimeoutExpired):
        return {"ok": False, "error": "Chromium setup could not finish. Check your connection and retry."}
    if result.returncode:
        return {"ok": False, "error": "Chromium download failed. Check your connection and available disk space."}
    return check_browser()


def check_browser() -> dict:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as driver:
            browser = driver.chromium.launch(**chromium_launch_options(driver.chromium))
            browser.close()
    except Exception:
        return {"ok": False, "error": "Chromium could not start. Check installation and operating-system dependencies."}
    return {"ok": True, "detail": "Chromium started successfully."}


def desktop_readiness() -> dict:
    if sys.platform == "win32":
        available = importlib.util.find_spec("pywinauto") is not None
        detail = "Windows UI Automation" if available else "This build does not include Windows UI Automation."
    elif sys.platform.startswith("linux"):
        available = importlib.util.find_spec("Xlib") is not None and bool(os.getenv("DISPLAY") or shutil.which("Xvfb"))
        detail = "Linux X11" if available else "Linux desktop tests need X11 support and an X11 display or Xvfb."
    else:
        available = False
        detail = "Native desktop testing is supported on Windows and Linux. Use browser or command tests here."
    return {"state": "available" if available else "unavailable", "detail": detail}
