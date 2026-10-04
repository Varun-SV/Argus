"""Argus desktop app — a pywebview window over the engine.

The UI (``web/index.html``) is a conversation. It talks to this Python API via
``window.pywebview.api``: typed messages go through :meth:`ArgusAPI.interpret`
(slash commands, or LLM routing for free text) and every card the UI draws is
backed by one of the methods below. Long work (runs, roam, watch) happens on
background threads; the UI polls :meth:`ArgusAPI.job_status` and
:meth:`ArgusAPI.live` while it runs.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import yaml

from argus import __version__
from argus.config import ArgusConfig, _env_bool, init_project, load_config
from argus.engine.results import load_runs
from argus.engine.spec import AssertStep, SpecError, discover_tests, load_spec, parse_spec
from argus.gui import assistant
from argus.providers.base import ProviderError
from argus.providers.registry import PROVIDER_TYPES
from argus.tokens import Budget, TokenTracker

WEB_DIR = Path(__file__).parent / "web"

_MAX_LOG_LINES = 400
_CAPSULE_LABELS = {"auto": "auto", "hyperv": "Hyper-V", "libvirt": "libvirt/KVM"}
# Roam engine execution_status -> card status.
_ROAM_STATUS = {"pass": "done", "fail": "fail", "error": "error", "cancelled": "stopped",
                "outcome_unknown": "unknown"}


def _forced_environment() -> Optional[str]:
    """ARGUS_EXECUTION_ENVIRONMENT wins over any per-session choice (see make_execution_environment)."""
    value = (os.environ.get("ARGUS_EXECUTION_ENVIRONMENT") or "").strip().lower()
    return value or None


class _StoppableBudget(Budget):
    """A run budget that also reports exhaustion when the user presses Stop.

    The runner and roam loop already check ``exhausted()`` between steps, so a
    stop request ends the session at the next step boundary without touching
    the engine.
    """

    def __init__(self, inner: Budget, stop: threading.Event) -> None:
        super().__init__(inner.max_seconds, inner.max_tokens, inner.tracker)
        self._stop_event = stop

    def exhausted(self) -> Optional[str]:
        if self._stop_event.is_set():
            return "stopped by you"
        return super().exhausted()


class ArgusAPI:
    """Methods exposed to the web UI. Every public method returns JSON-safe data."""

    def __init__(self, project_dir: Optional[Path] = None, *, project_required=False,
                 project_opener=None) -> None:
        self._project_dir = project_dir
        self._project_required = project_required
        self._project_opener = project_opener
        self._lock = threading.RLock()
        self._persist_lock = threading.Lock()
        self._stop = threading.Event()
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}
        self._job_tracker: Optional[TokenTracker] = None
        self._jobs: Dict[str, dict] = {}
        self._job_specs: Dict[str, dict] = {}       # job id -> {run index: TestSpec} parsed at start
        self._job_trackers: Dict[str, list] = {}    # job id -> TokenTrackers used by that job
        self._active_job: Optional[str] = None
        self._closing = False
        self._results: Dict[str, dict] = {}
        self._last_finished: Optional[str] = None
        self._last_failed: Optional[str] = None
        self._draft: Optional[dict] = None          # the most recent draft
        self._drafts: Dict[str, dict] = {}          # draft id -> draft, so cards act on their own
        self._last_target = ""
        self._latest_screenshot: Optional[bytes] = None
        self._latest_screenshot_ts = 0.0
        self._active_ks = None
        self._watch: Optional[dict] = None
        self._session = {
            "provider": None,           # None = config default
            "environment": None,        # None = config default
            "capsule_provider": None,
            "retain": None,
            "memory": True,
        }

    # ---- config / session ---------------------------------------------------

    def _config(self, provider: Optional[str] = None) -> ArgusConfig:
        if self._project_required:
            raise ValueError("Open a project folder before setting up or running tests.")
        try:
            return load_config(self._project_dir, provider=provider or self._session["provider"])
        except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
            # Parser diagnostics can contain credential-bearing YAML lines.
            raise ValueError("Could not load .argus/config.yaml. Check its YAML structure and setting values, then retry.") from None

    def open_project(self) -> dict:
        """Open a separate project window; never rebind an active job's project."""
        if self._project_opener is None:
            return {"ok": False, "error": "Project selection requires the Argus desktop window."}
        try:
            return self._project_opener()
        except (OSError, ValueError):
            return {"ok": False, "error": "Could not open that folder. Choose an existing project folder."}

    def _session_view(self, cfg: ArgusConfig) -> dict:
        """The effective environment, resolved the same way make_execution_environment does."""
        forced = _forced_environment()
        env = forced or self._session["environment"] or cfg.execution.environment or "local"
        cap = (self._session["capsule_provider"] or os.environ.get("ARGUS_CAPSULE_PROVIDER")
               or cfg.execution.capsule.provider or "auto")
        retain = self._session["retain"]
        if retain is None:
            try:
                retain = _env_bool("ARGUS_CAPSULE_RETAIN_ON_FAILURE",
                                   cfg.execution.capsule.retain_on_failure)
            except ValueError:
                retain = cfg.execution.capsule.retain_on_failure
        return {"environment": env, "capsule_provider": cap, "retain": bool(retain),
                "memory": bool(self._session["memory"]), "env_locked": forced is not None}

    def _job_environment(self, cfg: ArgusConfig, overrides: dict):
        """Return (env, capsule_provider, retain, error) for a new job."""
        s = self._session_view(cfg)
        env = overrides.get("environment") or s["environment"]
        forced = _forced_environment()
        if forced and env != forced:
            return None, None, None, (
                f"ARGUS_EXECUTION_ENVIRONMENT={forced} is set for this app, so Argus can't run "
                f"this {env}. Unset it to choose the environment here.")
        cap = overrides.get("capsule_provider") or s["capsule_provider"]
        retain = overrides.get("retain", s["retain"])
        return env, cap, bool(retain), None

    @staticmethod
    def _env_label(env: str, cap: str) -> str:
        if env == "capsule":
            return f"Capsule · {_CAPSULE_LABELS.get(cap, cap)}"
        return "Local"

    def app_info(self) -> dict:
        try:
            cfg = self._config()
            s = self._session_view(cfg)
        except (OSError, ValueError, TypeError, AttributeError, yaml.YAMLError):
            required = self._project_required
            return {
                "ok": False, "version": __version__, "project_required": required,
                "can_open_project": self._project_opener is not None,
                "project": str(self._project_dir or Path.cwd()) if not required else "",
                "project_name": "Open a project" if required else Path(self._project_dir or Path.cwd()).name,
                "initialized": False, "provider": "", "model": "Choose a project" if required else "Check configuration",
                "providers": [], "environment": "local", "capsule_provider": "auto",
                "env_label": "Local", "retain": False, "memory": True, "last_target": "",
                "tokens": self._usage_now(),
                "error": ("Open a folder to start testing. Each project keeps its own tests and conversations."
                          if required else "Could not load .argus/config.yaml. Check its YAML structure and setting values, then retry."),
            }
        return {
            "ok": True, "project_required": False, "can_open_project": self._project_opener is not None,
            "version": __version__,
            "project": str(cfg.project_dir),
            "project_name": cfg.project_dir.name,
            "initialized": cfg.argus_dir.is_dir(),
            "provider": cfg.provider.type,
            "model": cfg.provider.model,
            "providers": self._configured_providers(cfg),
            "time_minutes": cfg.time_minutes,
            "max_tokens": cfg.max_tokens,
            "environment": s["environment"],
            "capsule_provider": s["capsule_provider"],
            "env_label": self._env_label(s["environment"], s["capsule_provider"]),
            "retain": s["retain"],
            "memory": s["memory"],
            "last_target": self._last_target,
            "tokens": self._usage_now(),
        }

    @staticmethod
    def _configured_providers(cfg: ArgusConfig) -> List[dict]:
        entries = cfg.raw.get("providers") or {}
        out = []
        for name in PROVIDER_TYPES:
            entry = entries.get(name)
            if name != cfg.provider.type and not isinstance(entry, dict):
                continue
            entry = entry if isinstance(entry, dict) else {}
            model = cfg.provider.model if name == cfg.provider.type else str(entry.get("model") or "")
            if name == "ollama":
                note = "Local · " + str(entry.get("base_url") or "http://localhost:11434")
            elif entry.get("base_url"):
                note = "OpenAI-compatible · base_url"
            elif entry.get("api_key_env"):
                note = str(entry["api_key_env"])
            else:
                note = "configured"
            out.append({"type": name, "model": model, "note": note,
                        "current": name == cfg.provider.type})
        return out

    def set_provider(self, name: str) -> dict:
        cfg = self._config()
        names = [p["type"] for p in self._configured_providers(cfg)]
        if name not in names:
            return {"ok": False, "error": f"'{name}' is not configured in .argus/config.yaml"}
        pinned = (os.environ.get("ARGUS_PROVIDER") or "").strip()
        if pinned and name != pinned:
            return {"ok": False, "error": (
                f"ARGUS_PROVIDER={pinned} pins this app to {pinned}. "
                "Unset it before switching providers in the GUI.")}
        self._session["provider"] = name
        return {"ok": True, **self.app_info()}

    def set_environment(self, environment: str, capsule_provider: Optional[str] = None) -> dict:
        if environment not in assistant.ENVIRONMENTS:
            return {"ok": False, "error": f"unknown environment {environment!r}"}
        if capsule_provider is not None and capsule_provider not in assistant.CAPSULE_PROVIDERS:
            return {"ok": False, "error": f"unknown Capsule provider {capsule_provider!r}"}
        forced = _forced_environment()
        if forced and environment != forced:
            return {"ok": False, "error": (
                f"ARGUS_EXECUTION_ENVIRONMENT={forced} is set for this app, so runs stay {forced}. "
                "Unset it to choose the environment here.")}
        self._session["environment"] = environment
        if capsule_provider:
            self._session["capsule_provider"] = capsule_provider
        return {"ok": True, **self.app_info()}

    def set_retain(self, on: bool) -> dict:
        self._session["retain"] = bool(on)
        return {"ok": True, **self.app_info()}

    def set_memory(self, on: bool) -> dict:
        self._session["memory"] = bool(on)
        return {"ok": True, **self.app_info()}

    def environment(self) -> dict:
        cfg = self._config()
        s = self._session_view(cfg)
        cc = cfg.execution.capsule
        if s["environment"] == "capsule":
            rows = [
                {"k": "environment", "v": "capsule · disposable VM"},
                {"k": "provider", "v": {
                    "auto": "auto · Hyper-V on Windows, libvirt/KVM on Linux",
                    "hyperv": "Hyper-V · Windows guest",
                    "libvirt": "libvirt/QEMU/KVM · Linux guest",
                }.get(s["capsule_provider"], s["capsule_provider"])},
                {"k": "image source", "v": "verified ISO-derived cache" if cc.environment_definition else "manually prepared image"},
                {"k": "image", "v": cc.image_cache_root if cc.environment_definition else (cc.image or "not configured")},
                {"k": "guest control", "v": ("fresh generation · exact TLS pin · rotated bearer" if cc.environment_definition
                                              else f"{cc.guest_transport} · session bearer rotation {'on' if cc.rotate_session_token else 'off'}")},
                {"k": "network", "v": cc.network_mode},
                {"k": "retain_on_failure", "v": "on · keeps a Failure Capsule" if s["retain"] else "off"},
            ]
            note = ("No silent fallback: if Capsule requirements aren't met, the run stops "
                    "instead of running locally.")
            if cc.environment_definition:
                rows.append({"k": "definition", "v": cc.environment_definition})
            try:
                # Factory inspection verifies configuration/cache without prepare,
                # provider commands, VM allocation or guest contact.
                env = cfg.make_execution_environment("cli", "capsule", {
                    "provider": s["capsule_provider"], "retain_on_failure": s["retain"],
                })
                env._validate_provider_host_platform(env.provider)
                if cc.environment_definition:
                    env._validate_provisioned_security()
                    rows.append({"k": "environment ID", "v": env.settings.environment_id})
                elif not env.settings.image or not Path(env.settings.image).is_file():
                    raise ValueError("Configure an existing golden image in execution.capsule.image.")
                elif not env.settings.guest_token:
                    raise ValueError("Configure the manual guest's control credential using the documented host secret settings.")
                rows.append({"k": "configuration", "v": "verified · VM and guest readiness checked when a run starts"})
            except Exception as exc:
                rows.append({"k": "configuration", "v": "not ready"})
                note = f"{exc} No VM was created. " + note
        else:
            rows = [
                {"k": "environment", "v": "local · shared, non-isolated"},
                {"k": "Windows desktop input", "v": "semantic UI Automation (safe mode)"},
                {"k": "legacy physical input", "v": "off · explicit opt-in only"},
            ]
            note = "Fine for development. Use a Capsule when a test is destructive or production-bound."
        return {"label": self._env_label(s["environment"], s["capsule_provider"]),
                "rows": rows, "note": note, **s}

    # ---- chat ------------------------------------------------------------------

    def interpret(self, text: str) -> dict:
        """Turn a typed message into an intent for the UI to execute."""
        text = (text or "").strip()
        if not text:
            return {"intent": "none", "args": {}}
        tests = self.list_tests()
        try:
            parsed = assistant.parse_slash(text, tests, self._last_target)
        except assistant.IntentError as exc:
            return {"intent": "error", "args": {"text": str(exc)}}
        if parsed is not None:
            return parsed

        cfg = self._config()
        context = {
            "tests": [{"file": t["file"], "name": t["name"], "adapter": t["adapter"]} for t in tests],
            "last_target": self._last_target,
            "providers": [p["type"] for p in self._configured_providers(cfg)],
            "has_draft": self._draft is not None,
            "has_failed_run": self._last_failed is not None,
            "environment": self._session_view(cfg)["environment"],
        }
        tracker = TokenTracker()
        try:
            provider = cfg.make_provider(tracker)
            return assistant.route_with_llm(provider, text, context)
        except ProviderError as exc:
            return {"intent": "error", "args": {"text": (
                f"I couldn't reach {cfg.provider.type} to understand that ({exc}). "
                "Slash commands skip this routing step — type /help.")}}
        finally:
            self._charge(tracker, cfg)

    def help(self) -> dict:
        return {"groups": assistant.HELP_GROUPS}

    # ---- tests -----------------------------------------------------------------

    def list_tests(self) -> list:
        cfg = self._config()
        last: Dict[str, str] = {}
        for run in load_runs(cfg.project_dir, 100):
            last.setdefault(run.get("test_file", ""), run.get("status", ""))
        out = []
        for path in discover_tests(cfg.project_dir):
            entry = {"file": path.name, "name": path.stem, "steps": 0, "adapter": "?",
                     "error": None, "last": last.get(path.name)}
            try:
                spec = load_spec(path)
                entry.update(name=spec.name, steps=len(spec.steps), adapter=spec.adapter)
            except assistant.SPEC_ERRORS as exc:
                entry["error"] = str(exc)
            out.append(entry)
        return out

    def _test_path(self, cfg: ArgusConfig, file_name: str) -> Optional[Path]:
        for path in discover_tests(cfg.project_dir):
            if path.name == file_name:
                return path
        return None

    def read_test(self, file_name: str) -> dict:
        cfg = self._config()
        path = self._test_path(cfg, file_name)
        if path is None:
            return {"ok": False, "error": "not found"}
        return {"ok": True, "content": path.read_text(encoding="utf-8")}

    def _get_draft(self, draft_id: Optional[str]):
        """Return (draft, error). With an id, only that draft; without one, the latest."""
        if draft_id:
            draft = self._drafts.get(draft_id)
            if draft is None:
                return None, ("That draft is from an earlier session, so I no longer have it. "
                              "Ask me to write it again, then save that one.")
            return draft, None
        if self._draft is None:
            return None, "There's no drafted test yet. Ask me to write one first."
        return self._draft, None

    def _keep_draft(self, draft: dict) -> dict:
        draft["id"] = uuid.uuid4().hex[:12]
        self._drafts[draft["id"]] = draft
        self._draft = draft
        return draft

    def dry_run(self, tests="all", draft_id: Optional[str] = None) -> dict:
        cfg = self._config()
        items = []
        if tests == "draft":
            d, err = self._get_draft(draft_id)
            if err:
                return {"ok": False, "error": err}
            items.append({"file": d["file"], "adapter": d["adapter"], "launch": d.get("launch", ""),
                          "steps": d["steps"], "error": None})
            return {"ok": True, "items": items}
        paths = discover_tests(cfg.project_dir)
        if tests != "all":
            wanted = set(tests or [])
            paths = [p for p in paths if p.name in wanted]
        if not paths:
            return {"ok": False, "error": "No tests found in .argus/ — try /init to create an example."}
        for path in paths:
            try:
                spec = load_spec(path)
            except assistant.SPEC_ERRORS as exc:
                items.append({"file": path.name, "adapter": "?", "launch": "", "steps": [], "error": str(exc)})
                continue
            items.append({"file": path.name, "adapter": spec.adapter, "launch": spec.launch,
                          "steps": assistant.describe_steps(spec.steps), "error": None})
        return {"ok": True, "items": items}

    def draft_test(self, description: str) -> dict:
        cfg = self._config()
        tracker = TokenTracker()
        try:
            provider = cfg.make_provider(tracker)
            existing = [p.name for p in discover_tests(cfg.project_dir)]
            draft = assistant.draft_spec(provider, description, existing)
        except ProviderError as exc:
            return {"ok": False, "error": f"The model couldn't draft a test: {exc}"}
        finally:
            self._charge(tracker, cfg)
        draft["saved"] = False
        if draft["ok"]:
            self._keep_draft(draft)
        return draft

    def save_test(self, draft_id: Optional[str] = None) -> dict:
        """Save a draft (the one shown on the card, else the latest) as ``.argus/<file>``.

        Never overwrites an existing file.
        """
        cfg = self._config()
        draft, err = self._get_draft(draft_id)
        if err:
            return {"ok": False, "error": err}
        if draft.get("saved"):
            return {"ok": True, "path": f".argus/{draft['file']}", "file": draft["file"],
                    "draft_id": draft.get("id")}
        content, file_name = draft["yaml"], draft["file"]
        name = Path(str(file_name or "")).name
        if not name.endswith(".test.yaml") or name != file_name or name.startswith("."):
            return {"ok": False, "error": "Test files must be named <name>.test.yaml."}
        try:
            parse_spec(content)
        except assistant.SPEC_ERRORS as exc:
            return {"ok": False, "error": f"The spec doesn't parse: {exc}"}
        try:
            argus_dir = _argus_root(cfg, create=True)
            dest = argus_dir / name
            with open(dest, "x", encoding="utf-8") as fh:  # exclusive create: never overwrite
                fh.write(content)
        except FileExistsError:
            return {"ok": False, "error": f".argus/{name} already exists; I won't overwrite it."}
        except OSError as exc:
            return {"ok": False, "error": f"Could not save the test: {exc}"}
        draft["saved"] = True
        return {"ok": True, "path": f".argus/{name}", "file": name, "draft_id": draft.get("id")}

    def init_project(self) -> dict:
        cfg = self._config()
        _argus_root(cfg, create=True)
        notes = {"config.yaml": "provider, budgets, execution, knowledge",
                 "smoke.test.yaml": "CLI smoke test · no model calls required",
                 "runs": "run results + ATES evidence", "roam": "roam reports + regression stubs"}
        existed = {name for name in notes if (cfg.argus_dir / name).exists()}
        path = init_project(cfg.project_dir, create_example=False)
        sample = {
            "name": "CLI smoke test", "target": {"adapter": "cli", "launch": (
                "cmd.exe /d /c echo Argus is ready" if os.name == "nt" else "/bin/echo Argus is ready")},
            "steps": [{"assert": {"exit_code_is": 0}}, {"assert": {"stdout_contains": "Argus is ready"}}],
        }
        try:
            with (path / "smoke.test.yaml").open("x", encoding="utf-8") as handle:
                yaml.safe_dump(sample, handle, sort_keys=False)
        except FileExistsError:
            pass
        files = [
            {"path": f".argus/{name}" + ("/" if (path / name).is_dir() else ""), "note": note,
             "created": name not in existed}
            for name, note in notes.items() if (path / name).exists()
        ]
        return {"ok": True, "path": str(path), "files": files}

    # ---- jobs: run / roam ------------------------------------------------------

    def _begin_job(self, job: dict) -> Optional[str]:
        with self._lock:
            if self._closing:
                return "This window is closing. Reopen the project to start another job."
            if self._active_job and self._jobs[self._active_job]["running"]:
                return "Argus is already running something. Stop it first, or wait for it to finish."
            self._stop.clear()
            self._jobs[job["id"]] = job
            self._active_job = job["id"]
            self._latest_screenshot = None
            self._latest_screenshot_ts = 0.0
        return None

    def run_tests(self, tests="all", overrides: Optional[dict] = None) -> dict:
        """Start running ``tests`` ("all" or a list of file names) in one background job."""
        cfg = self._config()
        overrides = overrides or {}
        paths = discover_tests(cfg.project_dir)
        if tests != "all":
            order = list(tests or [])
            by_name = {p.name: p for p in paths}
            paths = [by_name[n] for n in order if n in by_name]
        if not paths:
            return {"ok": False, "error": "No tests to run. Try /init to create an example."}
        env, cap, retain, err = self._job_environment(cfg, overrides)
        if err:
            return {"ok": False, "error": err}
        runs = []
        specs = {}
        for path in paths:
            try:
                spec = load_spec(path)
                specs[len(runs)] = spec
                steps = [{"text": st.describe() if isinstance(st, AssertStep) else st.text,
                          "kind": st.kind} for st in spec.steps]
                runs.append({"file": path.name, "name": spec.name, "adapter": spec.adapter,
                             "launch": spec.launch, "planned": steps, "steps": [],
                             "status": "queued", "result": None, "key": None, "notes": []})
            except assistant.SPEC_ERRORS as exc:
                runs.append({"file": path.name, "name": path.stem, "adapter": "?", "launch": "",
                             "planned": [], "steps": [], "status": "error", "result": None,
                             "key": None, "notes": [f"spec error: {exc}"]})
        job = {"id": uuid.uuid4().hex[:12], "kind": "run", "running": True, "runs": runs,
               "env": env, "capsule_provider": cap, "retain": retain,
               "env_label": self._env_label(env, cap),
               "provider_type": cfg.provider.type,
               "provider": f"{cfg.provider.type}:{cfg.provider.model}",
               "action": "Starting…", "started_at": time.time(), "current": 0}
        err = self._begin_job(job)
        if err:
            return {"ok": False, "error": err}
        self._job_specs[job["id"]] = specs
        threading.Thread(target=self._run_worker, args=(job, cfg), daemon=True).start()
        return {"ok": True, "job": self.job_status(job["id"])}

    def _run_worker(self, job: dict, cfg: ArgusConfig) -> None:
        try:
            for index, run in enumerate(job["runs"]):
                job["current"] = index
                if run["status"] == "error":
                    self._record_run_error(job, run, run["notes"][-1] if run["notes"] else "run setup failed")
                    continue
                if self._stop.is_set():
                    run["status"] = "stopped"
                    continue
                self._execute_run(job, run, self._job_specs.get(job["id"], {}).get(index), cfg)
        finally:
            self._job_specs.pop(job["id"], None)
            job["ended_at"] = time.time()
            job["running"] = False
            job["action"] = "Finished"

    def _execute_run(self, job: dict, run: dict, spec, cfg: ArgusConfig) -> None:
        """Run ``spec`` — parsed when the job was created, so edits made meanwhile don't apply."""
        from argus.adapters import AdapterError
        from argus.engine.runner import run_test

        run["status"] = "running"
        job["action"] = f"Launching {run['launch']}"
        tracker = TokenTracker()
        self._job_tracker = tracker
        self._job_trackers.setdefault(job["id"], []).append(tracker)
        ks = None
        adapter = None
        try:
            ks = cfg.make_knowledge_store()
            self._active_ks = ks
            if spec is None:
                raise SpecError(f"{run['file']} could not be parsed when the run started")
            provider = cfg.make_provider(tracker)
            capsule = ({"provider": job["capsule_provider"], "retain_on_failure": job["retain"]}
                       if job["env"] == "capsule" else None)
            adapter = _ScreenshotCapturingAdapter(
                cfg.make_execution_environment(spec.adapter, job["env"], capsule), self
            )
            budget = _StoppableBudget(cfg.make_budget(tracker), self._stop)

            def on_step(sr) -> None:
                step = dict(vars(sr))
                run["steps"].append(step)
                job["action"] = (sr.actions[-1] if sr.actions else sr.text)

            result = run_test(
                spec, provider, adapter, budget,
                on_step=on_step,
                warn=lambda msg: run["notes"].append(msg),
                knowledge_store=ks,
                project_dir=cfg.project_dir,
            )
            try:
                result.save(cfg.project_dir)
            except OSError as exc:
                run["notes"].append(f"could not save result: {exc}")
            data = result.to_dict()
            data["ates_run_id"] = getattr(result, "ates_run_id", None)
            data["kind"] = "run"
            key = data["ates_run_id"] or uuid.uuid4().hex
            run["key"] = key
            run["result"] = data
            run["status"] = "stopped" if self._stop.is_set() and data["status"] != "pass" else data["status"]
            run["notes"].extend(_run_notes(data))
            with self._lock:
                self._results[key] = data
                self._last_finished = key
                if data["status"] != "pass":
                    self._last_failed = key
        except (SpecError, AdapterError, ProviderError, OSError, ValueError) as exc:
            run["status"] = "error"
            message = str(exc)
            run["notes"].append(message)
            self._record_run_error(job, run, message)
        except Exception as exc:  # e.g. a browser driver failing to start
            run["status"] = "error"
            message = f"{type(exc).__name__}: {exc}"
            run["notes"].append(message)
            self._record_run_error(job, run, message)
        finally:
            if ks is not None:
                job["stats"] = self.live_stats(run["launch"])
                try:
                    ks.close()
                except Exception:
                    run["notes"].append("Knowledge state could not be saved. Check the knowledge store before continuing.")
            self._active_ks = None
            self._job_tracker = None
            self._charge(tracker, cfg)

    def _record_run_error(self, job: dict, run: dict, message: str) -> None:
        """Make wrapper/setup failures the current explainable terminal result."""
        data = {
            "kind": "run",
            "test_file": run.get("file"),
            "status": "error",
            "error": message,
            "environment_type": job.get("env"),
            "steps": list(run.get("steps") or []),
            "ates_run_id": None,
        }
        key = "error-" + uuid.uuid4().hex
        run["key"] = key
        run["result"] = data
        with self._lock:
            self._results[key] = data
            self._last_finished = key
            self._last_failed = key

    def _record_roam_error(self, job: dict, message: str) -> None:
        """Make a failed roam the latest terminal result, so /evidence can't show an older job."""
        data = {"kind": "roam", "target": job.get("target"), "status": "error", "error": message,
                "ates_run_id": None, "findings": [], "report": None, "stopped_reason": message}
        key = "error-" + uuid.uuid4().hex
        job["key"] = key
        with self._lock:
            self._results[key] = data
            self._last_finished = key

    def start_roam(self, target: str, adapter: Optional[str] = None,
                   minutes: Optional[float] = None, memory: Optional[bool] = None,
                   overrides: Optional[dict] = None) -> dict:
        target = (target or "").strip()
        if not target:
            return {"ok": False, "error": "Tell Argus what to roam, e.g. notepad.exe or http://localhost:3000"}
        adapter = adapter or assistant.adapter_for(target)
        if adapter not in assistant.ADAPTERS:
            return {"ok": False, "error": f"unknown adapter {adapter!r}"}
        cfg = self._config()
        s = self._session_view(cfg)
        env, cap, retain, err = self._job_environment(cfg, overrides or {})
        if err:
            return {"ok": False, "error": err}
        memory = s["memory"] if memory is None else bool(memory)
        minutes = minutes or cfg.time_minutes or 10
        job = {"id": uuid.uuid4().hex[:12], "kind": "roam", "running": True, "target": target,
               "adapter": adapter, "minutes": float(minutes), "memory": memory,
               "env": env, "capsule_provider": cap, "retain": retain,
               "env_label": self._env_label(env, cap),
               "provider_type": cfg.provider.type,
               "provider": f"{cfg.provider.type}:{cfg.provider.model}",
               "log": [], "findings": [], "report": None, "regressions": [], "status": "running",
               "action": f"Launching {target}", "started_at": time.time(), "key": None,
               "stopped_reason": ""}
        err = self._begin_job(job)
        if err:
            return {"ok": False, "error": err}
        self._last_target = target
        threading.Thread(target=self._roam_worker, args=(job, cfg), daemon=True).start()
        return {"ok": True, "job": self.job_status(job["id"])}

    def _roam_worker(self, job: dict, cfg: ArgusConfig) -> None:
        from argus.adapters import AdapterError
        from argus.engine.roam import roam

        tracker = TokenTracker()
        self._job_tracker = tracker
        self._job_trackers.setdefault(job["id"], []).append(tracker)
        ks = None
        try:
            ks = cfg.make_knowledge_store()
            self._active_ks = ks
            provider = cfg.make_provider(tracker)
            capsule = ({"provider": job["capsule_provider"], "retain_on_failure": job["retain"]}
                       if job["env"] == "capsule" else None)
            adapter = _ScreenshotCapturingAdapter(
                cfg.make_execution_environment(job["adapter"], job["env"], capsule), self
            )
            budget = _StoppableBudget(
                cfg.make_budget(tracker, time_minutes=job["minutes"]), self._stop
            )
            session_dir = cfg.argus_dir / "roam" / time.strftime("%Y%m%d-%H%M%S")
            memory_dir = cfg.argus_dir / "roam" / "memory" if job["memory"] else None

            def on_event(line: str) -> None:
                log = job["log"]
                log.append(line)
                if len(log) > _MAX_LOG_LINES:
                    del log[: len(log) - _MAX_LOG_LINES]
                job["action"] = line

            session = roam(
                target=job["target"], provider=provider, adapter=adapter, budget=budget,
                session_dir=session_dir, on_event=on_event, stop_flag=self._stop.is_set,
                memory_dir=memory_dir, knowledge_store=ks, project_dir=cfg.project_dir,
            )
            job["findings"] = [
                {"title": f.title, "severity": f.severity, "expected": f.expected,
                 "actual": f.actual, "detail": f.detail}
                for f in session.findings
            ]
            job["report"] = _rel(cfg, session_dir / "report.md")
            job["regressions"] = [_rel(cfg, p) for p in sorted(session_dir.glob("regression-*.test.yaml"))]
            job["stopped_reason"] = session.stopped_reason or ""
            status = str(getattr(session, "execution_status", "") or "")
            job["status"] = ("stopped" if self._stop.is_set()
                             else _ROAM_STATUS.get(status, "unknown") if status else "done")
            data = {"kind": "roam", "target": job["target"], "status": job["status"],
                    "ates_run_id": getattr(session, "ates_run_id", None),
                    "findings": job["findings"], "report": job["report"],
                    "tokens": session.tokens, "stopped_reason": job["stopped_reason"]}
            key = data["ates_run_id"] or uuid.uuid4().hex
            job["key"] = key
            with self._lock:
                self._results[key] = data
                self._last_finished = key
        except (AdapterError, ProviderError, OSError, ValueError) as exc:
            job["log"].append(f"error: {exc}")
            job["status"] = "error"
            job["stopped_reason"] = str(exc)
            self._record_roam_error(job, str(exc))
        except Exception as exc:  # keep the UI honest instead of a silently dead thread
            job["log"].append(f"error: {type(exc).__name__}: {exc}")
            job["status"] = "error"
            job["stopped_reason"] = f"{type(exc).__name__}: {exc}"
            self._record_roam_error(job, job["stopped_reason"])
        finally:
            if ks is not None:
                job["stats"] = self.live_stats(job["target"])
                try:
                    ks.close()
                except Exception:
                    job["log"].append("Knowledge state could not be saved. Check the knowledge store before continuing.")
            self._active_ks = None
            self._job_tracker = None
            self._charge(tracker, cfg)
            job["ended_at"] = time.time()
            job["running"] = False
            job["action"] = "Session finished" if job["status"] == "done" else job["status"].capitalize()

    def regression_stub(self, job_id: str, index: int = 0) -> dict:
        """Load a roam finding's regression stub as a draft the user can save."""
        cfg = self._config()
        job = self._jobs.get(job_id)
        if not job or job.get("kind") != "roam" or not job.get("regressions"):
            return {"ok": False, "error": "That roam didn't produce regression stubs."}
        index = max(0, min(int(index), len(job["regressions"]) - 1))
        path = (cfg.project_dir / job["regressions"][index]).resolve()
        if not path.is_relative_to((cfg.argus_dir / "roam").resolve()) or not path.is_file():
            return {"ok": False, "error": "The regression stub is no longer on disk."}
        existing = [p.name for p in discover_tests(cfg.project_dir)]
        draft = assistant.check_draft(path.read_text(encoding="utf-8"), existing)
        draft["file"] = assistant.unique_file_name(path.name, existing)
        draft["saved"] = False
        draft["from_finding"] = True
        if draft.get("ok"):
            self._keep_draft(draft)
        return draft

    def stop(self) -> dict:
        """Stop the active run or roam at the next step boundary, and any watch."""
        self._stop.set()
        if self._watch:
            self._watch["running"] = False
        return {"ok": True}

    def job_status(self, job_id: str) -> dict:
        job = self._jobs.get(job_id)
        if job is None:
            return {"ok": False, "error": "unknown job"}
        snap = _copy(job)
        snap["ok"] = True
        snap["tokens"] = self._job_tokens(job_id)
        return snap

    def _job_tokens(self, job_id: str) -> int:
        """Tokens used by this job's own model calls (not the whole session)."""
        return sum(t.snapshot()["total_tokens"] for t in self._job_trackers.get(job_id, []))

    def live(self) -> dict:
        """What the live-view panel shows: the active (or last) job."""
        job = self._jobs.get(self._active_job) if self._active_job else None
        if job is None:
            return {"has": False}
        out = {"has": True, "id": job["id"], "kind": job["kind"], "running": job["running"],
               "action": job.get("action", ""), "env_label": job["env_label"],
               "adapter": job.get("adapter") or "", "tokens": self._job_tokens(job["id"]),
               "screenshot_ts": self._latest_screenshot_ts}
        if job["kind"] == "run":
            run = job["runs"][min(job["current"], len(job["runs"]) - 1)]
            total = max(1, len(run["planned"]))
            out.update(title=run["file"], status=run["status"], adapter=run["adapter"],
                       progress=round(100 * min(len(run["steps"]), total) / total))
            target = run["launch"]
        else:
            target = job["target"]
            elapsed = time.time() - job["started_at"]
            out.update(title=job["target"], status=job["status"],
                       progress=round(min(100, 100 * elapsed / (job["minutes"] * 60)))
                       if job["running"] else 100)
        stats = self.live_stats(target) if job["running"] else (job.get("stats") or {})
        out.update(states=stats.get("states"), transitions=stats.get("transitions"),
                   bugs=stats.get("bugs"))
        return out

    # ---- live preview -------------------------------------------------------

    def capture_live(self, since: float = 0.0) -> dict:
        """Return the latest screenshot as base64 PNG, only if newer than ``since``."""
        png = self._latest_screenshot
        ts = self._latest_screenshot_ts
        if png is None or ts <= float(since or 0):
            return {"b64": None, "ts": ts}
        return {"b64": base64.b64encode(png).decode("ascii"), "ts": ts}

    def _set_screenshot(self, png: bytes) -> None:
        self._latest_screenshot = png
        self._latest_screenshot_ts = time.time()

    def live_stats(self, target: str = "") -> dict:
        """Return live knowledge counts for ``target`` from the active session's store."""
        ks = self._active_ks
        if ks is None or not target:
            return {"active": ks is not None}
        try:
            s = ks.get_stats(target).get(target, {})
        except Exception:
            return {"active": True}
        return {"active": True, "states": s.get("states", 0), "transitions": s.get("transitions", 0),
                "bugs": s.get("bugs", s.get("bug_nodes", 0))}

    # ---- watch ------------------------------------------------------------------

    def watch_start(self) -> dict:
        cfg = self._config()
        with self._lock:
            if self._closing:
                return {"ok": False, "error": "This window is closing. Reopen the project to start watching."}
            if self._watch and self._watch["running"]:
                return {"ok": True, "watch": _copy(self._watch)}
            # Don't clear self._stop here: a Stop must still reach an active job.
            self._watch = {"id": uuid.uuid4().hex[:12], "running": True,
                           "pattern": ".argus/*.test.yaml", "events": [], "started_at": time.time()}
            threading.Thread(target=self._watch_worker, args=(self._watch, cfg.project_dir),
                             daemon=True).start()
            return {"ok": True, "watch": _copy(self._watch)}

    def watch_stop(self) -> dict:
        if self._watch:
            self._watch["running"] = False
        return {"ok": True}

    def watch_status(self) -> dict:
        return _copy(self._watch) if self._watch else {"running": False, "events": []}

    def _watch_worker(self, watch: dict, project_dir: Path, poll: float = 1.0) -> None:
        seen = _mtimes(project_dir)
        pending: Dict[str, dict] = {}  # changed file -> its event, until it has been re-run
        while watch["running"]:
            time.sleep(poll)
            current = _mtimes(project_dir)
            for name, m in current.items():
                if seen.get(name) != m and name not in pending:
                    event = {"at": time.strftime("%H:%M:%S"), "file": name,
                             "status": "running", "summary": "re-running…"}
                    watch["events"].append(event)
                    pending[name] = event
            for name in sorted(set(seen) - set(current)):
                # A deleted spec has nothing to re-run; record it so the sidebar refreshes.
                watch["events"].append({"at": time.strftime("%H:%M:%S"), "file": name, "change": "removed",
                                        "status": "removed", "summary": "spec removed"})
                pending.pop(name, None)
            seen = current
            for name in list(pending):
                if not watch["running"]:
                    break
                event = pending[name]
                started = self.run_tests([name])
                if not started.get("ok"):
                    active = self._jobs.get(self._active_job) if self._active_job else None
                    if active and active["running"]:
                        # Another run or roam holds the slot: keep the change, retry next poll.
                        event.update(status="waiting", summary="waiting for the current job to finish")
                        break
                    del pending[name]  # e.g. the spec was deleted: nothing to retry
                    event.update(status="skipped", summary=started.get("error", "not run"))
                    continue
                del pending[name]
                event.update(status="running", summary="re-running…")
                job = self._jobs[started["job"]["id"]]
                while job["running"]:
                    time.sleep(0.3)
                run = job["runs"][0]
                result = run.get("result") or {}
                event["status"] = run["status"]
                steps = result.get("steps", [])
                passed = sum(1 for s in steps if s.get("status") == "pass")
                event["summary"] = (f"{passed}/{len(steps)} steps passed" if steps
                                    else (run["notes"][-1] if run["notes"] else run["status"]))

    # ---- knowledge --------------------------------------------------------------

    def knowledge(self, target: str = "") -> dict:
        from argus.knowledge.fingerprint import target_key

        cfg = self._config()
        kc = cfg.knowledge
        backend = f"{kc.type} graph · {kc.vector_backend} vectors · {kc.embedding_model}"
        try:
            ks = cfg.make_knowledge_store()
        except Exception as exc:  # optional extras may be missing
            return {"ok": False, "error": f"Knowledge store unavailable: {exc}"}
        if ks is None:
            return {"ok": False, "error": "The knowledge store is disabled or its extras aren't installed "
                                          "(pip install \"argus-app-testing[knowledge]\")."}
        try:
            # List stored graphs from disk: get_stats(None) keys are Path.stem values
            # ("notepad-exe.graph"), which don't round-trip through the store.
            persist = Path(kc.persist_dir) if kc.persist_dir else cfg.argus_dir / "knowledge"
            targets = sorted(path.name[: -len(".graph.json")]
                             for path in persist.glob("*.graph.json"))
            target = (target or "").strip()
            if target and target_key(target) not in targets:
                target = _pick_target(target, targets) or target
            chosen = target or self._last_target or (targets[0] if targets else "")
            if not chosen:
                return {"ok": False, "error": "Argus hasn't learned anything yet. Roam an app to build its state graph."}
            if target_key(chosen) not in targets:
                # Querying an unknown target makes the store create (and on close, save) an
                # empty graph for it, so a typo would become a permanent "target".
                known = f" Known targets: {', '.join(targets)}." if targets else ""
                return {"ok": False, "error": f"Argus has no knowledge for {chosen!r} yet.{known}"}
            s = ks.get_stats(chosen).get(chosen, {})
        finally:
            ks.close()
        return {"ok": True, "target": chosen, "targets": targets, "backend": backend,
                "states": s.get("states", 0), "transitions": s.get("transitions", 0),
                "bugs": s.get("bugs", s.get("bug_nodes", 0)), "sessions": s.get("sessions", 0)}

    def knowledge_reset(self, target: str) -> dict:
        try:
            cfg = self._config()
            ks = cfg.make_knowledge_store()
            if ks is not None:
                ks.clear_target(target)
                ks.close()
            return {"ok": True, "target": target}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def knowledge_export(self, target: str) -> dict:
        from argus.knowledge.fingerprint import target_key

        cfg = self._config()
        key = target_key(target)
        kc = cfg.knowledge
        try:
            # The default store lives in the project, which may be untrusted: attest it.
            persist = (Path(kc.persist_dir) if kc.persist_dir
                       else _argus_subdir(cfg, "knowledge", create=False))
            graph = persist / f"{key}.graph.json"
            if graph.is_symlink():
                return {"ok": False, "error": f"{_rel(cfg, graph)} is a symlink; Argus won't export it."}
            if not graph.is_file():
                return {"ok": False, "error": f"No graph for '{target}' yet."}
            if not graph.resolve().is_relative_to(persist.resolve()):
                return {"ok": False, "error": f"{_rel(cfg, graph)} escapes the knowledge directory."}
            data = _read_nofollow(graph)
            dest = _argus_subdir(cfg, "exports") / f"{key}.graph.json"
            _write_atomic(dest, data)
        except OSError as exc:
            return {"ok": False, "error": f"Could not export the graph: {exc}"}
        return {"ok": True, "path": _rel(cfg, dest), "target": target}

    # ---- evidence / explain -----------------------------------------------------

    def evidence(self, key: Optional[str] = None) -> dict:
        from argus.ates import FinalizationError, RunId, verify_finalized_run
        from argus.ates.store import _run_directory_key

        key = key or self._last_finished
        data = self._results.get(key) if key else None
        if not data and key:
            # A card restored from an earlier session carries its ATES run id; the evidence
            # is still on disk even though this process has no in-memory result for it.
            try:
                data = {"ates_run_id": str(RunId(key)), "test_file": key}
            except (TypeError, ValueError):
                data = None
        if not data:
            return {"ok": False, "error": "No finished run in this conversation yet. Every run writes "
                                          "canonical ATES evidence; run a test and I can show it."}
        run_id = data.get("ates_run_id")
        title = data.get("test_file") or data.get("target") or "run"
        if not run_id:
            return {"ok": False, "error": f"{title} finished without an ATES run id, so there's no evidence to verify."}
        cfg = self._config()
        run_dir = cfg.argus_dir / "runs" / _run_directory_key(RunId(run_id))
        if not run_dir.is_dir():
            return {"ok": False, "error": f"No ATES evidence found for {run_id} under {_rel(cfg, run_dir.parent)}/."}
        out = {"ok": True, "title": title, "run_id": run_id, "path": _rel(cfg, run_dir) + "/",
               "verified": False, "rows": []}
        try:
            fin = verify_finalized_run(run_dir)
        except (FinalizationError, OSError, ValueError) as exc:
            out.update(state="invalid", headline="Evidence did not verify", detail=str(exc))
            return out
        manifest = json.loads(Path(fin.evidence_manifest_path).read_text(encoding="utf-8"))
        ev = manifest.get("evidence", {})
        artifacts = manifest.get("artifacts", [])
        out.update(
            verified=True, state=fin.trust_state.value,
            headline="Manifest verified · regenerated from canonical evidence",
            rows=[
                {"k": "effective status", "v": fin.outcome.effective_status.value},
                {"k": "ordered events", "v": str(ev.get("event_count", "?"))},
                {"k": "artifacts", "v": str(len(artifacts))},
                {"k": "evidence digest", "v": _short_digest(ev.get("sha256", ""))},
                {"k": "trust state", "v": fin.trust_state.value.replace("_", " ")},
                {"k": "report", "v": "derived from evidence, not the source of truth"},
            ],
            note=("Hashes detect corruption. Tamper-evidence needs an independent trust binding "
                  "such as a signature or immutable storage."),
        )
        return out

    def explain(self, key: Optional[str] = None, restored: Optional[dict] = None) -> dict:
        """Explain a failed run: ``key`` is an in-session result key or a history id from recent_runs.

        ``restored`` is the result saved on a conversation card. After a restart the new
        process has no in-memory result for that card's key (setup errors never reach disk),
        so the card's own result is used, reduced to the fields an explanation reads.
        """
        data = None
        if key and key.startswith("history:"):
            data = self._history_result(key[len("history:"):])
        else:
            key = key or self._last_failed
            data = self._results.get(key) if key else None
            if data is None and key and isinstance(restored, dict) and restored.get("kind") == "run":
                data = _restored_result(restored)
            if data is None and not key:
                for hid, run in self._history(20):
                    if run.get("status") != "pass":
                        data = dict(run, kind="run")
                        key = "history:" + hid
                        break
        if not data or data.get("kind") != "run":
            return {"ok": False, "error": "Nothing has failed in this conversation yet. Run a test and I "
                                          "can explain any failure step by step."}
        cfg = self._config()
        tracker = TokenTracker()
        try:
            provider = cfg.make_provider(tracker)
            text = assistant.explain_failure(provider, data)
        except ProviderError as exc:
            return {"ok": False, "error": f"The model couldn't explain it: {exc}"}
        finally:
            self._charge(tracker, cfg)
        # Evidence is verifiable for runs with an ATES run id: this session's, or a restored
        # card's (its key is that id). Persisted history rows don't carry one.
        evidence_key = key if data.get("ates_run_id") and (
            key in self._results or data.get("ates_run_id") == key) else None
        return {"ok": True, "text": text, "test": data.get("test_file"), "key": key,
                "evidence_key": evidence_key}

    # ---- providers / tokens / history -------------------------------------------

    def check_provider(self) -> dict:
        cfg = self._config()
        tracker = TokenTracker()
        try:
            provider = cfg.make_provider(tracker)
            status = provider.check_connection()
        except ProviderError as exc:
            status = {"ok": False, "detail": str(exc)}
        finally:
            self._charge(tracker, cfg)
        status["providers"] = self._configured_providers(cfg)
        status["provider"] = cfg.provider.type
        status["model"] = cfg.provider.model
        return status

    def _charge(self, tracker: TokenTracker, cfg: ArgusConfig) -> None:
        """Add one tracker's usage to this session and persist it exactly once."""
        snap = tracker.snapshot()
        if not snap["calls"]:
            return
        with self._lock:
            for k in self._usage:
                self._usage[k] += snap.get(k, 0)
        if cfg.argus_dir.is_dir():
            # persist() is a read-modify-write of usage.json; serialize it across threads.
            with self._persist_lock:
                try:
                    tracker.persist(cfg.project_dir)
                except OSError:
                    pass

    def _usage_now(self) -> dict:
        with self._lock:
            out = dict(self._usage)
        live = self._job_tracker
        if live is not None:
            snap = live.snapshot()
            for k in out:
                out[k] += snap.get(k, 0)
        return out

    def token_usage(self) -> dict:
        cfg = self._config()
        persisted = TokenTracker.load_persisted(cfg.project_dir)
        return {"session": self._usage_now(), "project": persisted,
                "provider": cfg.provider.type, "max_tokens": cfg.max_tokens}

    def _history(self, limit: int):
        """(id, result) pairs for persisted runs, newest first — the same files load_runs reads."""
        try:
            runs_dir = _argus_subdir(self._config(), "runs", create=False)
        except OSError:
            return []
        out = []
        for path in sorted(runs_dir.glob("*.json"), reverse=True)[:limit]:
            try:
                if path.is_symlink():
                    continue
                resolved = path.resolve(strict=True)
                resolved.relative_to(runs_dir)
                out.append((path.name, json.loads(resolved.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, OSError, ValueError):
                continue
        return out

    def _history_result(self, history_id: str) -> Optional[dict]:
        name = Path(history_id).name
        if name != history_id or not name.endswith(".json") or name.startswith("."):
            return None
        try:
            runs_dir = _argus_subdir(self._config(), "runs", create=False)
            path = runs_dir / name
            if path.is_symlink():
                return None
            resolved = path.resolve(strict=True)
            resolved.relative_to(runs_dir)
            data = json.loads(resolved.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return dict(data, kind="run") if isinstance(data, dict) else None

    def recent_runs(self, limit: int = 20) -> list:
        rows = []
        for hid, r in self._history(limit):
            steps = r.get("steps", [])
            rows.append({
                "id": "history:" + hid,
                "test": r.get("test_file", "?"), "status": r.get("status", "?"),
                "steps": f"{sum(1 for s in steps if s.get('status') == 'pass')}/{len(steps)}",
                "duration": round(float(r.get("duration_s") or 0), 1),
                "tokens": (r.get("tokens") or {}).get("total_tokens", 0),
                "provider": r.get("provider", ""), "started_at": r.get("started_at"),
            })
        return rows

    # ---- conversations -----------------------------------------------------------

    def load_conversations(self) -> list:
        cfg = self._config()
        try:
            path = _conversation_path(cfg)
            if path.is_symlink():
                return []
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else []

            # One-time migration from the PR's earlier project-local location.
            # The legacy path is attested before reading and removed only after
            # the user-data copy succeeds.
            try:
                legacy = _argus_subdir(cfg, "gui", create=False) / "conversations.json"
            except OSError:
                return []
            if legacy.is_symlink() or not legacy.is_file():
                return []
            data = json.loads(legacy.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return []
            data = data[:30]
            _write_atomic(path, json.dumps(data).encode("utf-8"))
            try:
                legacy.unlink()
            except OSError:
                pass
            return data
        except (OSError, json.JSONDecodeError):
            return []

    def save_conversations(self, conversations: list) -> dict:
        try:
            path = _conversation_path(self._config())
            _write_atomic(path, json.dumps(list(conversations or [])[:30]).encode("utf-8"))
        except OSError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}


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
            self._api._set_screenshot(obs.screenshot_png)
        return obs

    def launch(self, target: str):
        return self._inner.launch(target)

    def act(self, action: dict) -> str:
        return self._inner.act(action)

    def close(self) -> None:
        return self._inner.close()


def _copy(value):
    return json.loads(json.dumps(value, default=str))


def _rel(cfg: ArgusConfig, path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(cfg.project_dir.resolve()).as_posix()
    except ValueError:
        return str(path)


def _short_digest(digest: str) -> str:
    d = digest.split(":", 1)[-1]
    return f"sha256 · {d[:4]}…{d[-4:]}" if len(d) > 8 else (digest or "?")


def _pick_target(query: str, targets: List[str]) -> Optional[str]:
    if not query:
        return None
    q = query.lower().replace("https://", "").replace("http://", "")
    for t in targets:
        if t.lower() == query.lower():
            return t
    for t in targets:
        tl = t.lower()
        if q in tl or tl in q:
            return t
    return None


def _run_notes(data: dict) -> List[str]:
    notes = []
    env = data.get("environment_type") or "local"
    notes.append(f"Environment {env}" + (" · isolated" if data.get("isolated") else " · shared host"))
    if data.get("artifacts"):
        notes.append(f"Collected {len(data['artifacts'])} artifact(s) · SHA-256 hashed, bounded")
    fc = data.get("failure_capsule")
    if fc:
        notes.append(f"Failure Capsule kept: {fc.get('failure_id', fc.get('vm_name', 'retained'))} "
                     "(disk + config only, no live credentials)")
    if data.get("failure_capsule_error"):
        notes.append("Failure Capsule retention failed; the Capsule was preserved for recovery")
    if data.get("transfer_error"):
        notes.append(f"Transfer error: {data['transfer_error']}")
    if data.get("error"):
        notes.append(data["error"])
    return notes


def _conversation_path(cfg: ArgusConfig) -> Path:
    """Per-user GUI state path, keyed by project without storing chats in its repo."""
    override = os.environ.get("ARGUS_GUI_STATE_DIR")
    if override:
        root = Path(override).expanduser()
    elif os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
                    or (Path.home() / "AppData" / "Local")) / "Argus"
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "Argus"
    else:
        root = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")) / "argus"

    project = str(Path(cfg.project_dir).resolve())
    project_key = hashlib.sha256(project.encode("utf-8")).hexdigest()[:24]
    directory = root / "projects" / project_key
    directory.mkdir(parents=True, exist_ok=True)
    return directory / "conversations.json"


def _argus_root(cfg: ArgusConfig, create: bool = False) -> Path:
    """Return an attested project-local ``.argus`` root."""
    project_root = Path(cfg.project_dir).resolve(strict=True)
    argus_dir = project_root / ".argus"
    if argus_dir.is_symlink():
        raise OSError(f".argus cannot be a symlink: {argus_dir}")
    if create:
        argus_dir.mkdir(exist_ok=True)
    elif not argus_dir.is_dir():
        raise OSError("no .argus directory")
    resolved = argus_dir.resolve(strict=True)
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise OSError(f".argus escapes the project: {argus_dir}") from exc
    return resolved


def _argus_subdir(cfg: ArgusConfig, name: str, create: bool = True) -> Path:
    """``.argus/<name>`` inside the project, refusing symlinks that would redirect writes.

    Mirrors the runs-root attestation in argus.engine.results: a project may be untrusted,
    so neither ``.argus`` nor the subdirectory may point outside it.
    """
    project_root = Path(cfg.project_dir).resolve(strict=True)
    argus_dir = _argus_root(cfg, create=False)
    sub = argus_dir / name
    if sub.is_symlink():
        raise OSError(f".argus/{name} cannot be a symlink: {sub}")
    if create:
        sub.mkdir(exist_ok=True)
    resolved = sub.resolve(strict=create)
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise OSError(f".argus/{name} escapes the project: {sub}") from exc
    return resolved


def _restored_result(card_result: dict) -> dict:
    """The fields of a card's saved run result that an explanation uses, nothing else."""
    steps = [dict((k, st.get(k)) for k in ("index", "kind", "text", "status", "expected",
                                           "actual", "note", "actions"))
             for st in (card_result.get("steps") or [])[:200] if isinstance(st, dict)]
    out = {k: card_result.get(k) for k in ("test_file", "status", "error", "environment_type")}
    rid = card_result.get("ates_run_id")
    out.update(kind="run", steps=steps, ates_run_id=rid if isinstance(rid, str) else None)
    return out


def _read_nofollow(path: Path) -> bytes:
    """Read ``path`` without following a symlink planted in its place (O_NOFOLLOW)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as fh:
        return fh.read()


def _write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` via a fresh, exclusively created temp file.

    The temp name is random and opened with O_EXCL (and O_NOFOLLOW where available), so a
    planted symlink is never followed; os.replace then swaps the directory entry itself.
    """
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
    fd = os.open(tmp, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _mtimes(project_dir: Path) -> Dict[str, float]:
    out = {}
    for path in discover_tests(project_dir):
        try:
            out[path.name] = path.stat().st_mtime
        except OSError:
            continue
    return out


def run_gui() -> None:
    import webview
    persist_lock = threading.Lock()

    def create_project_window(project=None):
        api = ArgusAPI(project, project_required=project is None)
        # Windows can open the same project twice. Serialize usage persistence
        # across all its windows, as well as within each API instance.
        api._persist_lock = persist_lock
        window = webview.create_window(
            "Argus" + (f" · {project.name}" if project else ""),
            url=str(WEB_DIR / "index.html"), js_api=api,
            width=1440, height=900, min_size=(1024, 680), background_color="#FAF9F5",
        )

        def open_project():
            selected = window.create_file_dialog(webview.FileDialog.FOLDER)
            if not selected:
                return {"ok": True, "cancelled": True}
            folder = Path(selected[0]).resolve(strict=True)
            if not folder.is_dir():
                raise ValueError("not a directory")
            create_project_window(folder)
            return {"ok": True, "project": str(folder)}

        api._project_opener = open_project

        def closing():
            with api._lock:
                job = api._jobs.get(api._active_job) if api._active_job else None
                if job and job.get("running"):
                    api.stop()
                    blocked = True
                else:
                    api._closing = True
                    api.watch_stop()
                    blocked = False
            if blocked:
                try:
                    window.evaluate_js("window.dispatchEvent(new Event('arguscloseblocked'))")
                except Exception:
                    # A reloading/unavailable WebView must not bypass teardown.
                    pass
                return False
            return True

        window.events.closing += closing
        return window

    current = Path.cwd().resolve()
    create_project_window(current if (current / ".argus").is_dir() else None)
    webview.start()
