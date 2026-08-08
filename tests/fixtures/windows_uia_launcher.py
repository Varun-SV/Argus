"""Spawn the native UIA fixture as a child, then exit like a WinUI launcher."""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

TITLE = sys.argv[1] if len(sys.argv) > 1 else "Argus Descendant UI Target"
TARGET = Path(__file__).with_name("windows_uia_target.py")

# Keep the launcher alive briefly so Argus has a deterministic window in which
# to snapshot/prove the child relationship. The UI child intentionally outlives
# this process, matching common bootstrapper/WinUI launcher behavior.
subprocess.Popen([sys.executable, str(TARGET), TITLE])
time.sleep(1.25)
