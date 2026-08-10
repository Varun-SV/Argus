"""Argus desktop app — a pywebview window over the engine.

The UI (``web/index.html``) talks to this Python API via ``window.pywebview.api``.
Long work (runs, roam) happens on background threads; the UI polls for status.
"""

from __future__ import annotations

import base64
import threading
import time
from pathlib import Path
from typing import Optional

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
        self._latest_screenshot: Optional[bytes] = None
        self._active_ks = None  # live knowledge store during a session

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
        import time as _time

        cfg = load_config()
        state = self._run_state
        ks = cfg.make_knowledge_store()
        self._active_ks = ks
        try:
            spec = load_spec(cfg.argus_dir / file_name)
            provider = cfg.make_provider(self._tracker)
            adapter = _ScreenshotCapturingAdapter(create_adapter(spec.adapter), self)
            budget = cfg.make_budget(self._tracker)
            stamp = _time.strftime("%Y%m%d-%H%M%S")
            safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in file_name)
            shots_dir = cfg.argus_dir / "runs" / f"{stamp}-{safe}" / "shots"
            result = run_test(
                spec, provider, adapter, budget,
                on_step=lambda sr: state["steps"].append(vars(sr)),
                warn=lambda msg: state["steps"].append(
                    {"kind": "warn", "text": msg, "status": "skipped",
                     "index": -1, "duration_s": 0, "actions": [],
                     "expected": None, "actual": None, "note": None}
                ),
                knowledge_store=ks,
                shots_dir=shots_dir,
                project_dir=cfg.project_dir,
            )
            result.save(cfg.project_dir)
            state["result"] = result.to_dict()
        except (SpecError, AdapterError, ProviderError, OSError) as exc:
            state["result"] = {"status": "error", "error": str(exc)}
        finally:
            if ks is not None:
                ks.close()
            self._active_ks = None
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
        ks = cfg.make_knowledge_store()
        self._active_ks = ks
        try:
            provider = cfg.make_provider(self._tracker)
            adapter = _ScreenshotCapturingAdapter(create_adapter("desktop-gui"), self)
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
                knowledge_store=ks,
            )
            state["findings"] = len(session.findings)
            state["report"] = str(session_dir / "report.md")
        except (AdapterError, ProviderError, OSError) as exc:
            state["log"].append(f"error: {exc}")
        finally:
            if ks is not None:
                ks.close()
            self._active_ks = None
            self._tracker.persist(cfg.project_dir)
            state["running"] = False

    def stop_roam(self) -> dict:
        self._roam_stop = True
        return {"ok": True}

    def roam_status(self) -> dict:
        state = dict(self._roam_state)
        state["tokens"] = self._tracker.snapshot()
        return state

    # ---- live preview -------------------------------------------------------

    def capture_live(self) -> dict:
        """Return the latest captured screenshot as a base64 PNG."""
        png = self._latest_screenshot
        if png is None:
            return {"b64": None, "ts": 0}
        return {
            "b64": base64.b64encode(png).decode("ascii"),
            "ts": time.time(),
        }

    # ---- knowledge ----------------------------------------------------------

    def knowledge_stats(self, target: str) -> dict:
        try:
            cfg = load_config()
            ks = cfg.make_knowledge_store()
            if ks is None:
                return {"states": 0, "transitions": 0, "bugs": 0, "sessions": 0}
            stats = ks.get_stats(target)
            ks.close()
            return stats.get(target, {"states": 0, "transitions": 0, "bugs": 0, "sessions": 0})
        except Exception as exc:
            return {"error": str(exc), "states": 0, "transitions": 0, "bugs": 0, "sessions": 0}

    def knowledge_clear(self, target: str) -> dict:
        try:
            cfg = load_config()
            ks = cfg.make_knowledge_store()
            if ks is not None:
                ks.clear_target(target)
                ks.close()
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def live_stats(self) -> dict:
        """Return live knowledge counts from the active session."""
        ks = self._active_ks
        if ks is None:
            return {"active": False}
        try:
            stats_map = ks.get_stats()
            total = {"states": 0, "transitions": 0, "bugs": 0}
            for v in stats_map.values():
                total["states"] += v.get("states", 0)
                total["transitions"] += v.get("transitions", 0)
                total["bugs"] += v.get("bugs", v.get("bug_nodes", 0))
            total["active"] = True
            return total
        except Exception:
            return {"active": True}

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


class _ScreenshotCapturingAdapter:
    """Thin wrapper that caches the latest screenshot for the live preview."""

    def __init__(self, inner, api: ArgusAPI) -> None:
        self._inner = inner
        self._api = api

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def observe(self, include_screenshot: bool = True):
        obs = self._inner.observe(include_screenshot=include_screenshot)
        if obs.screenshot_png:
            self._api._latest_screenshot = obs.screenshot_png
        return obs

    def launch(self, target: str):
        return self._inner.launch(target)

    def act(self, action: dict) -> str:
        return self._inner.act(action)

    def close(self) -> None:
        return self._inner.close()


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
