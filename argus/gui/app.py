"""Argus desktop app — a pywebview window over the engine.

The UI (``web/index.html``) is built on the Argus design system and talks
to this Python API via ``window.pywebview.api``. Long work (runs, roam)
happens on background threads; the UI polls for status.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from argus import __version__
from argus.config import init_project, load_config
from argus.engine.results import load_runs
from argus.engine.spec import SpecError, discover_tests, load_spec
from argus.providers.base import ProviderError
from argus.tokens import TokenTracker

WEB_DIR = Path(__file__).parent / "web"


class ArgusAPI:
    """Methods exposed to the web UI."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tracker = TokenTracker()
        self._run_state: dict = {"running": False, "steps": [], "result": None, "test": None}
        self._roam_state: dict = {"running": False, "log": [], "report": None, "findings": 0}
        self._roam_stop = False

    # ---- project / config -------------------------------------------------

    def app_info(self) -> dict:
        cfg = load_config()
        return {
            "version": __version__,
            "project": str(cfg.project_dir),
            "provider": cfg.provider.type,
            "model": cfg.provider.model,
            "time_minutes": cfg.time_minutes,
            "max_tokens": cfg.max_tokens,
        }

    def init_project(self) -> dict:
        path = init_project()
        return {"ok": True, "path": str(path)}

    # ---- tests -----------------------------------------------------------

    def list_tests(self) -> list:
        cfg = load_config()
        out = []
        for path in discover_tests(cfg.project_dir):
            entry = {"file": path.name, "name": path.stem, "steps": 0, "adapter": "?", "error": None}
            try:
                spec = load_spec(path)
                entry.update(name=spec.name, steps=len(spec.steps), adapter=spec.adapter)
            except SpecError as exc:
                entry["error"] = str(exc)
            out.append(entry)
        return out

    def read_test(self, file_name: str) -> dict:
        cfg = load_config()
        path = cfg.argus_dir / file_name
        if not path.is_file() or path.suffix not in (".yaml", ".yml"):
            return {"ok": False, "error": "not found"}
        return {"ok": True, "content": path.read_text(encoding="utf-8")}

    def recent_runs(self, limit: int = 20) -> list:
        cfg = load_config()
        return load_runs(cfg.project_dir, limit)

    # ---- run -----------------------------------------------------------------

    def run_test(self, file_name: str) -> dict:
        with self._lock:
            if self._run_state["running"] or self._roam_state["running"]:
                return {"ok": False, "error": "a session is already running"}
            self._run_state = {"running": True, "steps": [], "result": None, "test": file_name}
        threading.Thread(target=self._run_worker, args=(file_name,), daemon=True).start()
        return {"ok": True}

    def _run_worker(self, file_name: str) -> None:
        from argus.adapters import AdapterError, create_adapter
        from argus.engine.runner import run_test

        cfg = load_config()
        state = self._run_state
        try:
            spec = load_spec(cfg.argus_dir / file_name)
            provider = cfg.make_provider(self._tracker)
            adapter = create_adapter(spec.adapter)
            budget = cfg.make_budget(self._tracker)
            result = run_test(
                spec, provider, adapter, budget,
                on_step=lambda sr: state["steps"].append(vars(sr)),
                warn=lambda msg: state["steps"].append(
                    {"kind": "warn", "text": msg, "status": "skipped",
                     "index": -1, "duration_s": 0, "actions": [],
                     "expected": None, "actual": None, "note": None}
                ),
            )
            result.save(cfg.project_dir)
            state["result"] = result.to_dict()
        except (SpecError, AdapterError, ProviderError, OSError) as exc:
            state["result"] = {"status": "error", "error": str(exc)}
        finally:
            self._tracker.persist(cfg.project_dir)
            state["running"] = False

    def run_status(self) -> dict:
        state = dict(self._run_state)
        state["tokens"] = self._tracker.snapshot()
        return state

    # ---- roam --------------------------------------------------------------------

    def start_roam(self, target: str, minutes: float, max_tokens) -> dict:
        with self._lock:
            if self._run_state["running"] or self._roam_state["running"]:
                return {"ok": False, "error": "a session is already running"}
            if not target.strip():
                return {"ok": False, "error": "enter a command to launch, e.g. notepad.exe"}
            self._roam_state = {"running": True, "log": [], "report": None, "findings": 0}
            self._roam_stop = False
        threading.Thread(
            target=self._roam_worker, args=(target, minutes, max_tokens), daemon=True
        ).start()
        return {"ok": True}

    def _roam_worker(self, target: str, minutes: float, max_tokens) -> None:
        from argus.adapters import AdapterError, create_adapter
        from argus.engine.roam import roam

        cfg = load_config()
        state = self._roam_state
        try:
            provider = cfg.make_provider(self._tracker)
            adapter = create_adapter("desktop-gui")
            budget = cfg.make_budget(
                self._tracker,
                time_minutes=minutes or None,
                max_tokens=int(max_tokens) if max_tokens else None,
            )
            stamp = time.strftime("%Y%m%d-%H%M%S")
            session_dir = cfg.argus_dir / "roam" / stamp
            session = roam(
                target=target,
                provider=provider,
                adapter=adapter,
                budget=budget,
                session_dir=session_dir,
                on_event=lambda line: state["log"].append(line),
                stop_flag=lambda: self._roam_stop,
            )
            state["findings"] = len(session.findings)
            state["report"] = str(session_dir / "report.md")
        except (AdapterError, ProviderError, OSError) as exc:
            state["log"].append(f"error: {exc}")
        finally:
            self._tracker.persist(cfg.project_dir)
            state["running"] = False

    def stop_roam(self) -> dict:
        self._roam_stop = True
        return {"ok": True}

    def roam_status(self) -> dict:
        state = dict(self._roam_state)
        state["tokens"] = self._tracker.snapshot()
        return state

    # ---- providers / tokens ---------------------------------------------------------

    def check_provider(self) -> dict:
        cfg = load_config()
        try:
            provider = cfg.make_provider(self._tracker)
        except ProviderError as exc:
            return {"ok": False, "detail": str(exc)}
        return provider.check_connection()

    def token_usage(self) -> dict:
        cfg = load_config()
        persisted = TokenTracker.load_persisted(cfg.project_dir)
        session = self._tracker.snapshot()
        return {"session": session, "project": persisted}


def run_gui() -> None:
    import webview

    api = ArgusAPI()
    webview.create_window(
        "Argus",
        url=str(WEB_DIR / "index.html"),
        js_api=api,
        width=1280,
        height=820,
        min_size=(960, 640),
        background_color="#080b12",
    )
    webview.start()
