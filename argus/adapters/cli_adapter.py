"""CLI/terminal adapter — runs commands and observes stdout/stderr/exit code."""
from __future__ import annotations

import shlex
import subprocess
from typing import Optional

from argus.adapters.base import Adapter, AdapterError, Observation


class CLIAdapter(Adapter):
    """
    Runs shell commands; all assertions are text-based (stdout, stderr, exit code).
    No vision — screenshots are never captured.
    """

    type_name = "cli"

    def __init__(self, timeout: int = 60, shell: bool = False) -> None:
        self._command: str = ""
        self._timeout = timeout
        self._shell = shell
        self._cwd: Optional[str] = None
        self._last_obs = Observation(window_title="")

    def set_working_directory(self, path: str) -> None:
        """Pin all subprocesses to an execution-environment-owned workspace."""
        self._cwd = str(path) if path else None

    def capabilities(self) -> dict:
        return {
            "actions": {
                "run": {"command": "required"},
                "execute": {"command": "required"},
                "wait": {},
                "done": {},
            },
            "notes": ["Use run/execute with an explicit command; there is no GUI input."],
        }

    def launch(self, target: str) -> None:
        self._command = target
        self._run(target)

    def launch_literal(self, target: str) -> None:
        """Execute one exact staged path without command-string reparsing."""
        self._command = str(target)
        self._run(str(target), literal=True)

    def observe(self, include_screenshot: bool = True) -> Observation:
        return self._last_obs

    def act(self, action: dict) -> str:
        kind = (action.get("action") or "").lower()
        if kind in ("run", "execute"):
            return self._run(action["command"])
        if kind == "done":
            return "done"
        if kind == "wait":
            import time
            time.sleep(min(float(action.get("seconds", 1)), 30))
            return "waited"
        raise AdapterError(f"CLI adapter: unknown action '{kind}' — use run/execute/done")

    def _run(self, cmd: str, *, literal: bool = False) -> str:
        if not cmd:
            cmd = self._command
        try:
            if literal:
                args = [str(cmd)]
                use_shell = False
            else:
                args = shlex.split(cmd) if not self._shell else cmd
                use_shell = self._shell
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                shell=use_shell,
                cwd=self._cwd,
            )
            self._last_obs = Observation(
                window_title=f"cli: {cmd[:60]}",
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                process_alive=True,
            )
            preview = result.stdout[:80].strip() or result.stderr[:80].strip()
            return f"exit {result.returncode} | {preview!r}"
        except subprocess.TimeoutExpired:
            self._last_obs = Observation(
                window_title=f"cli: {cmd[:60]}",
                stderr=f"command timed out after {self._timeout}s",
                exit_code=-1,
                process_alive=False,
            )
            return f"timeout after {self._timeout}s"
        except FileNotFoundError:
            self._last_obs = Observation(
                window_title=f"cli: {cmd[:60]}",
                stderr=f"command not found: {cmd.split()[0] if cmd else '(empty)'}",
                exit_code=127,
                process_alive=False,
            )
            return f"command not found: {cmd!r}"
        except Exception as exc:
            raise AdapterError(f"CLI run failed: {exc}") from exc

    def close(self) -> None:
        pass
