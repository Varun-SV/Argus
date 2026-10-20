"""Export golden fixtures for compatibility contracts C-02 (state) and C-06 (secrets).

The real Python writers build every persisted file under ``tests/golden/state``
and the real Python readers record what they make of it in ``*/expected/``:

* ``history/``       run results written by ``argus.engine.results.RunResult.save``
  (allocation-ordered names, the ``.storage-order`` project counter, flat history
  files, ``report.md``) plus a few hand-made legacy/malformed rows; read back with
  ``ArgusAPI.recent_runs``, ``load_runs`` and ``argus report``;
* ``tokens/``        ``.argus/usage.json`` written by ``TokenTracker.persist`` and the
  desktop ``ArgusAPI._charge`` path; read back with ``ArgusAPI.token_usage`` and
  ``argus tokens``;
* ``conversations/`` the per-user desktop conversation store written by
  ``ArgusAPI.save_conversations`` (busy-chat retention) and the legacy
  project-local migration, read back with ``ArgusAPI.load_conversations``;
* ``knowledge/``     ``<key>.graph.json``, ``<key>.states.ndjson`` and
  ``<key>.bugs.ndjson`` written by the JSON knowledge store (graph only: no
  chromadb, sentence-transformers, networkx, model or network access);
* ``secrets/``       the per-user SQLite secret store holding DUMMY values only,
  ``SECRET://ARGUS/NAME`` reference syntax cases and ``argus secrets list``.

Usage::

    python scripts/golden/export_state.py --write   # regenerate tests/golden/state
    python scripts/golden/export_state.py --check   # exit 1 and name every drifting file

Everything runs in a child process with a scrubbed environment: ``HOME`` and the
XDG directories point into a fresh temporary directory, no ``ARGUS_*`` or provider
variables are inherited, sockets are refused and no provider is constructed.

Determinism: the worker freezes the wall clock (``time.time``/``time.time_ns``),
``uuid.uuid4``, ``secrets.token_bytes`` (secret store key and nonces) and the secret
store's ``datetime.now`` with documented monkeypatches, and fixes each desktop
window's conversation identity (``ArgusAPI._conversation_identity``, the value the
window pins when it opens) to a fake path such as ``/golden/project``. Absolute
temporary paths are replaced by tokens like ``<PROJECT>`` and ``<STATE_HOME>``.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import difflib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

SCHEMA = "argus-golden-state-v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "tests" / "golden" / "state"
MANIFEST_NAME = "_manifest.json"
README_NAME = "README.md"
SECTIONS = ("history", "tokens", "conversations", "knowledge", "secrets")

# Python sources whose behaviour the generated files describe. Their hashes are
# recorded in the manifest so a change to a writer or reader shows up as drift.
SOURCES = (
    "argus/cli.py",
    "argus/config.py",
    "argus/engine/results.py",
    "argus/gui/app.py",
    "argus/gui/state.py",
    "argus/knowledge/__init__.py",
    "argus/knowledge/fingerprint.py",
    "argus/knowledge/json_store.py",
    "argus/knowledge/storage.py",
    "argus/secrets.py",
    "argus/tokens.py",
)

# Variables copied into the worker's environment. Everything else (ARGUS_*,
# provider API keys, proxies, real HOME/XDG locations) is deliberately dropped.
PASSTHROUGH_ENV = ("PATH", "SYSTEMROOT", "VIRTUAL_ENV")

# Fixed wall clock: 2026-01-01T00:00:00Z.
T0 = 1767225600.0
# 32 bytes, self-labelled. TEST ONLY key material for the dummy secret store.
TEST_ONLY_KEY = b"TEST ONLY golden key; not secret"
DUMMY_VALUE = "golden-dummy-not-a-secret"
FAKE_PROJECT = "/golden/project"
FAKE_LEGACY_PROJECT = "/golden/legacy-project"
FAKE_NON_LIST_PROJECT = "/golden/non-list-project"


# --------------------------------------------------------------------------- shared helpers


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_hashes() -> dict[str, str]:
    hashes = {}
    for rel in SOURCES:
        data = (REPO_ROOT / rel).read_bytes().replace(b"\r\n", b"\n")
        hashes[rel] = _sha256(data)
    return hashes


def _combined_source_hash(hashes: dict[str, str]) -> str:
    text = "".join(f"{rel}\0{hashes[rel]}\n" for rel in sorted(hashes))
    return _sha256(text.encode("utf-8"))


def render(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def sqlite_dump(path: Path) -> dict[str, Any]:
    """Logical content of a SQLite file: schema and every row (bytes as hex).

    SQLite page bytes depend on the library version, so the secret store is
    compared through this dump instead of byte for byte.
    """
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        schema = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        tables = {}
        for kind, name, _tbl, _sql in schema:
            if kind != "table":
                continue
            cursor = connection.execute(f'SELECT * FROM "{name}" ORDER BY 1')
            columns = [item[0] for item in cursor.description]
            rows = [
                [value.hex() if isinstance(value, bytes) else value for value in row]
                for row in cursor.fetchall()
            ]
            tables[name] = {"columns": columns, "rows": rows}
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    return {
        "schema": [list(row) for row in schema],
        "tables": tables,
        "user_version": user_version,
    }


# --------------------------------------------------------------------------- worker side


class _Normalizer:
    """Replaces temporary absolute paths with stable tokens."""

    def __init__(self, pairs: list[tuple[Path, str]]) -> None:
        unique: dict[str, str] = {}
        for path, token in pairs:
            for spelling in {str(path), str(Path(path).resolve())}:
                unique.setdefault(spelling, token)
        self._pairs = sorted(unique.items(), key=lambda item: -len(item[0]))

    def text(self, value: str) -> str:
        for needle, token in self._pairs:
            value = value.replace(needle, token)
        return value

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Path):
            return self.text(value.as_posix())
        if isinstance(value, dict):
            return {self.text(str(k)): self.value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        return value


class _Clock:
    """Frozen, explicitly advanced wall clock installed over time.time/time.time_ns."""

    def __init__(self, start: float) -> None:
        self.now = start

    def set(self, seconds: float) -> None:
        self.now = seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def time(self) -> float:
        return self.now

    def time_ns(self) -> int:
        return round(self.now * 1_000_000) * 1000


class _Determinism:
    """Documented monkeypatches that make every writer reproducible."""

    def __init__(self) -> None:
        import secrets as _secrets
        import time as _time
        import uuid as _uuid

        self.clock = _Clock(T0)
        self._uuid_counter = 0
        self._nonce_counter = 0
        _time.time = self.clock.time  # type: ignore[assignment]
        _time.time_ns = self.clock.time_ns  # type: ignore[assignment]
        _uuid.uuid4 = self._uuid4  # type: ignore[assignment]
        _secrets.token_bytes = self._token_bytes  # type: ignore[assignment]

    def _uuid4(self):
        import uuid as _uuid

        self._uuid_counter += 1
        seed = f"argus-golden-state-uuid-{self._uuid_counter}".encode("ascii")
        return _uuid.UUID(bytes=hashlib.sha256(seed).digest()[:16], version=4)

    def _token_bytes(self, n: int | None = None) -> bytes:
        if n == len(TEST_ONLY_KEY):
            return TEST_ONLY_KEY
        self._nonce_counter += 1
        seed = f"argus-golden-state-nonce-{self._nonce_counter}".encode("ascii")
        return hashlib.sha256(seed).digest()[: n or 32]


def _block_network() -> None:
    import socket

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("network access is disabled during golden export")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.getaddrinfo = refuse  # type: ignore[assignment]


def _attempt(fn: Callable[[], Any], norm: _Normalizer) -> dict[str, Any]:
    try:
        return {"value": norm.value(fn())}
    except Exception as exc:  # noqa: BLE001 - failures are part of the contract
        return {"error": {"message": norm.text(str(exc)), "type": type(exc).__name__}}


def _cli(args: list[str], cwd: Path, norm: _Normalizer, stdin: str | None = None) -> dict:
    """Run the real ``argus`` click command group in-process from ``cwd``."""
    from click.testing import CliRunner

    from argus.cli import main

    previous = Path.cwd()
    os.chdir(cwd)
    try:
        result = CliRunner().invoke(main, args, input=stdin, catch_exceptions=True)
    finally:
        os.chdir(previous)
    exception = None
    if result.exception is not None and not isinstance(result.exception, SystemExit):
        exception = {"message": norm.text(str(result.exception)),
                     "type": type(result.exception).__name__}
    return {
        "args": ["argus", *args],
        "exception": exception,
        "exit_code": result.exit_code,
        "stderr": norm.text(result.stderr),
        "stdout": norm.text(result.stdout),
    }


def _new_project(work: Path, name: str, config: str | None = None) -> Path:
    project = work / name
    (project / ".argus").mkdir(parents=True)
    if config is not None:
        (project / ".argus" / "config.yaml").write_text(config, encoding="utf-8")
    return project


def _copy_tree(src: Path, dest: Path) -> None:
    """Copy regular files only (empty directories carry no state)."""
    for path in sorted(src.rglob("*")):
        if path.is_file() and not path.is_symlink():
            target = dest / path.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)


class _Out:
    """Collects the files a section contributes to tests/golden/state."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def expected(self, section: str, name: str, api: Any, value: Any, **extra: Any) -> None:
        payload = {"api": api, "case": f"{section}/{name}", "schema": SCHEMA, "value": value}
        payload.update(extra)
        path = self.root / section / "expected" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(payload), encoding="utf-8")


# ----------------------------------------------------------------- history (C-02)


def _history(work: Path, out: _Out, det: _Determinism, env_norm: list[tuple[Path, str]]) -> None:
    from argus.engine.results import (
        RunResult,
        StepResult,
        _bounded_name,
        load_runs,
        run_history_paths,
        valid_run_history,
    )
    from argus.gui.app import ArgusAPI, _run_card_status

    project = _new_project(work, "history-project")
    norm = _Normalizer([(project, "<PROJECT>"), *env_norm])
    clock = det.clock
    written: list[dict[str, Any]] = []

    def make(test_file: str, started: float, status: str, steps: list[StepResult],
             tokens: dict[str, int], claim: bool = True, **fields: Any) -> RunResult:
        clock.set(started)
        result = RunResult(test_name=test_file.split("/")[-1].replace(".test.yaml", ""),
                           test_file=test_file, adapter=fields.pop("adapter", "desktop"),
                           provider=fields.pop("provider", "ollama"),
                           started_at=started, **fields)
        result.steps = steps
        result.status = status
        result.tokens = tokens
        result.duration_s = round(sum(step.duration_s for step in steps) + 0.35, 3)
        clock.advance(1.0)
        if claim:
            result.claim_project_order(project)
        clock.advance(0.5)
        return result

    def save(label: str, result: RunResult, note: str) -> None:
        path = result.save(project)
        written.append({"label": label, "note": note,
                        "history_id": path.parent.name + ".json",
                        "status": result.status})

    def tok(prompt: int, completion: int, calls: int) -> dict[str, int]:
        return {"prompt_tokens": prompt, "completion_tokens": completion,
                "total_tokens": prompt + completion, "calls": calls}

    save("pass", make("login.test.yaml", T0, "pass", [
        StepResult(0, "launch", "launch the app", "pass", 1.25, ["launch app.exe"]),
        StepResult(1, "act", "sign in as the golden user", "pass", 3.5,
                   ["click 4", "type 'golden-user'", "key enter"]),
        StepResult(2, "assert", "window title contains 'Dashboard'", "pass", 0.25,
                   expected="Dashboard", actual="Dashboard - Golden App"),
    ], tok(1200, 300, 4)), "plain passing run")

    save("fail", make("checkout.test.yaml", T0 + 60, "fail", [
        StepResult(0, "launch", "launch the app", "pass", 1.0),
        StepResult(1, "assert", "text 'Order placed' is visible", "fail", 2.0,
                   ["click 7"], expected="Order placed", actual="Payment declined",
                   screenshot_path="screenshots/step-2.png"),
        StepResult(2, "teardown", "close the app", "pass", 0.5),
    ], tok(800, 150, 2), environment_type="capsule", isolated=True,
        location="capsule/hyperv",
        failure_capsule={"failure_id": "golden-failure-1", "vm_name": "argus-golden-vm",
                         "vm_state": "Off", "root_dir": "capsules/golden-failure-1",
                         "reason": "test failure"}), "failing assertion with a retained Capsule")

    save("error", make("settings.test.yaml", T0 + 120, "error", [
        StepResult(0, "launch", "launch the app", "error", 0.75,
                   note="adapter error: process exited with code 3"),
        StepResult(1, "act", "open settings", "skipped", 0.0,
                   note="skipped after failure (continue_on_failure: false)"),
    ], tok(0, 0, 0), claim=False, error="RuntimeError: process exited with code 3",
        transfer_error="artifact collection failed: guest path missing",
        staged_files=[{"source": "fixtures/input.txt", "destination": "C:\\ArgusStage\\input.txt",
                       "size": 12, "sha256": "0" * 64}],
        artifacts=[{"path": "C:\\ArgusStage\\out.log", "host_path": "artifacts/out.log",
                    "size": 34, "sha256": "f" * 64}],
        failure_capsule_error={"error": "retention timed out", "vm_name": "argus-golden-vm-2",
                               "root_dir": "capsules/golden-failure-2",
                               "recovery": "inspect the preserved Capsule manually"}),
        "error run saved without claim_project_order (order reserved at save time)")

    save("cancelled", make("login.test.yaml", T0 + 180, "fail", [
        StepResult(0, "launch", "launch the app", "pass", 1.0),
        StepResult(1, "act", "sign in as the golden user", "skipped", 0.0,
                   note="skipped: stopped by you"),
    ], tok(100, 20, 1)), "run stopped by the user: stored as status 'fail'")

    for label, status in (("same-second-1", "pass"), ("same-second-2", "fail")):
        steps = [StepResult(0, "act", "search for 'golden'", status, 1.0)]
        save(label, make("search.test.yaml", T0 + 240, status, steps, tok(50, 10, 1)),
             "two runs of the same test file started in the same second")

    clock.set(T0 + 300)
    resaved = make("resave.test.yaml", T0 + 300, "running", [
        StepResult(0, "launch", "launch the app", "pass", 1.0)], tok(10, 2, 1))
    save("running-then-pass", resaved, "saved while running, then re-saved as pass")
    resaved.steps.append(StepResult(1, "assert", "process is running", "pass", 0.5))
    resaved.status = "pass"
    resaved.tokens = tok(20, 4, 2)
    resaved.save(project)
    written[-1]["status"] = "pass"

    # Committed names stay ASCII and short (Windows MAX_PATH, macOS normalization); the
    # 120-byte digest bound for long or non-ASCII names is recorded as a pure function.
    save("odd-name", make("suite/odd name & co.test.yaml", T0 + 360, "pass", [
        StepResult(0, "act", "noop", "pass", 0.25)], tok(5, 1, 1)),
        "unsafe characters in the test file name replaced by '_'")

    save("clock-rollback", make("rollback.test.yaml", T0 - 3600, "pass", [
        StepResult(0, "act", "noop", "pass", 0.25)], tok(5, 1, 1)),
        "wall clock moved back an hour: the stamp is older, the order is newest")

    runs = project / ".argus" / "runs"
    counter_value = (runs / ".storage-order").read_text(encoding="ascii")
    # Hand-made rows (not produced by today's writers): a legacy pre-allocation name,
    # and malformed newest rows that readers must skip without counting them.
    legacy = {"test_file": "legacy.test.yaml", "test_name": "legacy", "status": "pass",
              "adapter": "desktop", "provider": "ollama", "duration_s": 2,
              "tokens": {"total_tokens": 7}, "steps": [{"status": "pass"}],
              "started_at": T0 - 86400}
    synthesized = {
        "20251231-000000-legacy.test.yaml.json": json.dumps(legacy, indent=2),
        "20260102-000000-99999999999999999999-" + "a" * 32 + "-broken.test.yaml.json":
            "{ this is not json",
        "20260102-000000-99999999999999999998-" + "b" * 32 + "-cancelled.test.yaml.json":
            json.dumps({"test_file": "cancelled.test.yaml", "status": "cancelled"}, indent=2),
        "20260102-000000-99999999999999999997-" + "c" * 32 + "-list.test.yaml.json": "[]",
    }
    for name, text in synthesized.items():
        (runs / name).write_text(text, encoding="utf-8")

    _copy_tree(project / ".argus", out.root / "history" / "project" / ".argus")

    api = ArgusAPI(project_dir=project)
    paths = run_history_paths(runs)
    documents = []
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            data = None
        valid = valid_run_history(data)
        documents.append({
            "card_status": _run_card_status(data) if valid else None,
            "history_id": path.name,
            "valid_run_history": valid,
        })
    origin = {item["history_id"]: item for item in written}
    out.expected("history", "recent_runs", "argus.gui.app.ArgusAPI.recent_runs", {
        "limit_20_default": norm.value(api.recent_runs()),
        "limit_3": norm.value(api.recent_runs(3)),
    }, note="Newest first by allocation order; malformed rows are skipped and do not count "
            "toward the limit; legacy names sort after every allocation-ordered name.")
    out.expected("history", "load_runs", "argus.engine.results.load_runs", {
        "limit_3": norm.value(load_runs(project, 3)),
        "limit_50_default": norm.value(load_runs(project)),
    })
    out.expected("history", "history_order", "argus.engine.results.run_history_paths", {
        "counter_file": {"name": ".storage-order", "value": counter_value},
        "ordered": [
            dict(doc, writer=origin.get(doc["history_id"], {
                "label": "synthesized", "note": "hand-made row, not written by RunResult.save"}))
            for doc in documents
        ],
        "bounded_name": {name: _bounded_name(name) for name in (
            "login.test.yaml", "suite/odd name & co.test.yaml", "Ünïcode name.test.yaml",
            "suite/" + "very-long-segment-" * 8 + ".test.yaml",
            "ü" * 61 + ".test.yaml")},
    }, note="card_status is argus.gui.app._run_card_status (a stopped run shows 'stopped'); "
            "bounded_name is argus.engine.results._bounded_name (directory name suffix).")
    out.expected("history", "report_command", "argus report", {
        "default_limit_10": _cli(["report"], project, norm),
        "limit_3": _cli(["report", "--limit", "3"], project, norm),
    }, environment={"COLUMNS": os.environ.get("COLUMNS")})


# ------------------------------------------------------------------ tokens (C-02)


TOKENS_CONFIG = """\
# Golden fixture (tests/golden/state): token usage project.
provider: ollama
budgets:
  max_tokens: 50000
knowledge:
  enabled: false
"""


def _tokens(work: Path, out: _Out, env_norm: list[tuple[Path, str]]) -> None:
    from argus.config import load_config
    from argus.gui.app import ArgusAPI
    from argus.tokens import TokenTracker, Usage

    project = _new_project(work, "tokens-project", TOKENS_CONFIG)
    norm = _Normalizer([(project, "<PROJECT>"), *env_norm])
    usage_path = project / ".argus" / "usage.json"
    steps: list[dict[str, Any]] = []

    calls = [Usage(1200, 300), Usage(800, 200), Usage(0, 0)]
    run_tracker = TokenTracker()
    for usage in calls:
        run_tracker.add(usage)
    run_tracker.persist(project)
    steps.append({"step": "argus run: TokenTracker.persist after three calls",
                  "tracker_snapshot": run_tracker.snapshot(),
                  "usage_json": usage_path.read_text(encoding="utf-8")})

    second = TokenTracker()
    second.add(Usage(50, 25))
    second.persist(project)
    steps.append({"step": "second invocation merges into usage.json",
                  "tracker_snapshot": second.snapshot(),
                  "usage_json": usage_path.read_text(encoding="utf-8")})

    api = ArgusAPI(project_dir=project)
    cfg = load_config(project)
    job_tracker = TokenTracker()
    job_tracker.add(Usage(10, 5))
    job_tracker.add(Usage(30, 15))
    api._charge(job_tracker, cfg)
    api._charge(TokenTracker(), cfg)  # zero calls: neither counted nor persisted
    steps.append({"step": "desktop job charged through ArgusAPI._charge (plus an idle tracker)",
                  "tracker_snapshot": job_tracker.snapshot(),
                  "usage_json": usage_path.read_text(encoding="utf-8")})

    _copy_tree(project / ".argus", out.root / "tokens" / "project" / ".argus")

    edge_cases = []
    for name, text in (
        ("corrupt-json", "{ not json"),
        ("missing-total-tokens", json.dumps({"prompt_tokens": 3, "completion_tokens": 4,
                                             "calls": 1})),
        ("extra-keys", json.dumps({"prompt_tokens": 1, "completion_tokens": 2,
                                   "total_tokens": 3, "calls": 1, "note": "kept"})),
        ("json-list", "[]"),
    ):
        edge = _new_project(work, f"tokens-{name}", TOKENS_CONFIG)
        edge_norm = _Normalizer([(edge, "<PROJECT>"), *env_norm])
        edge_usage = edge / ".argus" / "usage.json"
        edge_usage.write_text(text, encoding="utf-8")
        record: dict[str, Any] = {
            "case": name,
            "usage_json_before": text,
            "load_persisted": _attempt(lambda edge=edge: TokenTracker.load_persisted(edge),
                                       edge_norm),
            "argus_tokens": _cli(["tokens"], edge, edge_norm),
        }
        tracker = TokenTracker()
        tracker.add(Usage(5, 5))
        record["persist_one_call"] = _attempt(lambda edge=edge, t=tracker: t.persist(edge),
                                              edge_norm)
        record["usage_json_after"] = edge_usage.read_text(encoding="utf-8")
        edge_cases.append(record)

    out.expected("tokens", "token_usage", "argus.gui.app.ArgusAPI.token_usage", {
        "per_call": [{"prompt_tokens": u.prompt_tokens, "completion_tokens": u.completion_tokens,
                      "total_tokens": u.total_tokens} for u in calls],
        "persistence_steps": steps,
        "token_usage": norm.value(api.token_usage()),
        "load_persisted": norm.value(TokenTracker.load_persisted(project)),
    }, note="session = this desktop window's charged jobs; project = .argus/usage.json "
            "(cumulative across processes). Per-run totals are RunResult.tokens "
            "(see history/).")
    out.expected("tokens", "tokens_command", "argus tokens", {
        "project": _cli(["tokens"], project, norm),
        "edge_cases": edge_cases,
    }, environment={"COLUMNS": os.environ.get("COLUMNS")})


# ----------------------------------------------------------- conversations (C-02)


def _chat(index: int, busy: Any = False, title: str | None = None) -> dict[str, Any]:
    cid = f"chat-{index:02d}"
    msgs: list[dict[str, Any]] = [
        {"id": f"{cid}-m1", "role": "user", "kind": "text", "text": "/run login.test.yaml"},
        {"id": f"{cid}-m2", "role": "argus", "kind": "text", "text": f"Golden reply {index}."},
    ]
    if busy is True:
        msgs.append({"id": f"{cid}-m3", "role": "argus", "kind": "run", "job": "job-golden",
                     "idx": 0, "snap": {"status": "running", "test": "login.test.yaml"}})
    return {"busy": busy, "id": cid, "msgs": msgs, "seq": len(msgs) + 1,
            "title": title or f"Golden chat {index}",
            "updated": int(T0 * 1000) - index * 60_000}


def _conversations(work: Path, out: _Out, env_norm: list[tuple[Path, str]],
                   state_home: Path) -> None:
    from argus.gui.app import ArgusAPI, _bounded_conversations, _conversation_path
    from argus.gui.state import gui_state_root, project_identity

    def api_for(project: Path, identity: str, **kwargs: Any) -> ArgusAPI:
        api = ArgusAPI(project_dir=project, **kwargs)
        # The window pins its identity when it opens; a fixed fake path keeps the
        # committed per-user key independent of this temporary directory.
        api._conversation_identity = identity
        return api

    def key_for(identity: str) -> str:
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]

    project = _new_project(work, "conversations-project")
    norm = _Normalizer([(project, "<PROJECT>"), *env_norm])

    chats = [_chat(i) for i in range(31)]
    chats[0]["title"] = "Résumé ✓ non-ASCII title"
    chats[30] = _chat(30, busy=True)
    chats.append(_chat(31, busy="true"))
    chats.append(_chat(32, busy=1))
    api = api_for(project, FAKE_PROJECT)
    saved = api.save_conversations(chats)
    store = _conversation_path(type("Cfg", (), {"project_dir": project})(), identity=FAKE_PROJECT)
    loaded = api.load_conversations()
    reopened = api_for(project, FAKE_PROJECT).load_conversations()

    legacy_project = _new_project(work, "conversations-legacy-project")
    legacy_chats = [_chat(i) for i in range(32)]
    legacy_file = legacy_project / ".argus" / "gui" / "conversations.json"
    legacy_file.parent.mkdir(parents=True)
    legacy_text = json.dumps(legacy_chats)
    legacy_file.write_text(legacy_text, encoding="utf-8")
    legacy_api = api_for(legacy_project, FAKE_LEGACY_PROJECT)
    migrated = legacy_api.load_conversations()

    non_list_project = _new_project(work, "conversations-non-list-project")
    non_list_store = state_home / "argus" / "projects" / key_for(FAKE_NON_LIST_PROJECT)
    non_list_store.mkdir(parents=True, exist_ok=True)
    (non_list_store / "conversations.json").write_text('{"not": "a list"}', encoding="utf-8")
    non_list = api_for(non_list_project, FAKE_NON_LIST_PROJECT).load_conversations()

    required = ArgusAPI(project_dir=project, project_required=True)

    _copy_tree(state_home, out.root / "conversations" / "xdg-state")
    inputs = out.root / "conversations" / "inputs" / "legacy-project" / ".argus" / "gui"
    inputs.mkdir(parents=True, exist_ok=True)
    (inputs / "conversations.json").write_text(legacy_text, encoding="utf-8")

    def summary(items: Any) -> Any:
        if not isinstance(items, list):
            return items
        return [{"busy": c.get("busy"), "id": c.get("id")} for c in items]

    old_override = os.environ.get("ARGUS_GUI_STATE_DIR")
    old_state = os.environ.get("XDG_STATE_HOME")
    roots = {}
    try:
        roots["XDG_STATE_HOME set"] = norm.value(gui_state_root())
        os.environ["ARGUS_GUI_STATE_DIR"] = "/golden/gui-state-override"
        roots["ARGUS_GUI_STATE_DIR=/golden/gui-state-override"] = norm.value(gui_state_root())
        del os.environ["ARGUS_GUI_STATE_DIR"]
        os.environ["XDG_STATE_HOME"] = "relative/state"
        roots["XDG_STATE_HOME=relative/state"] = norm.value(gui_state_root())
        del os.environ["XDG_STATE_HOME"]
        roots["XDG_STATE_HOME unset"] = norm.value(gui_state_root())
    finally:
        for name, old in (("ARGUS_GUI_STATE_DIR", old_override), ("XDG_STATE_HOME", old_state)):
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old

    out.expected("conversations", "load_conversations",
                 "argus.gui.app.ArgusAPI.load_conversations", {
                     "busy_retention": {
                         "input_count": len(chats),
                         "input_busy_flags": summary(chats)[29:],
                         "bounded_ids": [c["id"] for c in _bounded_conversations(chats)],
                         "save_conversations": saved,
                         "store_path": norm.value(store),
                         "load_conversations": loaded,
                         "reopened_window_load": [c["id"] for c in reopened],
                     },
                     "legacy_project_local_migration": {
                         "input": "inputs/legacy-project/.argus/gui/conversations.json",
                         "input_count": len(legacy_chats),
                         "load_conversations_ids": [c["id"] for c in migrated],
                         "legacy_file_removed": not legacy_file.exists(),
                         "store_path": norm.value(_conversation_path(
                             type("Cfg", (), {"project_dir": legacy_project})(),
                             identity=FAKE_LEGACY_PROJECT)),
                     },
                     "non_list_store": {"identity": FAKE_NON_LIST_PROJECT,
                                        "load_conversations": non_list},
                     "project_required_window": {
                         "load_conversations": required.load_conversations(),
                         "save_conversations": required.save_conversations([_chat(0)]),
                     },
                 })
    out.expected("conversations", "identity", "argus.gui.state", {
        "gui_state_root": roots,
        "identity_derivation": {
            "conversation_store": "<gui_state_root>/projects/<project_key>/conversations.json",
            "project_key": "sha256(utf-8 identity).hexdigest()[:24]",
            "project_identity": "os.path.normcase(str(Path(project).resolve(strict=True)))",
        },
        "keys": {identity: key_for(identity) for identity in
                 (FAKE_PROJECT, FAKE_LEGACY_PROJECT, FAKE_NON_LIST_PROJECT)},
        "project_identity_of_temp_project": norm.value(project_identity(project)),
    }, note="The exporter pins ArgusAPI._conversation_identity to the fake paths above; a real "
            "window pins project_identity(project) when it opens.")


# --------------------------------------------------------------- knowledge (C-02)


KNOWLEDGE_CONFIG = """\
# Golden fixture (tests/golden/state): graph-only JSON knowledge store.
provider: ollama
knowledge:
  enabled: true
  type: json
"""


def _knowledge(work: Path, out: _Out, det: _Determinism,
               env_norm: list[tuple[Path, str]]) -> None:
    from argus.adapters.base import Observation, UIElement
    from argus.config import load_config
    from argus.gui.app import ArgusAPI
    from argus.knowledge.fingerprint import target_key

    project = _new_project(work, "knowledge-project", KNOWLEDGE_CONFIG)
    norm = _Normalizer([(project, "<PROJECT>"), *env_norm])
    clock = det.clock
    clock.set(T0 + 7200)

    def obs(title: str, names: list[tuple[str, str]], **kwargs: Any) -> Observation:
        elements = [UIElement(i + 1, kind, name, (0, 0, 10, 10)) for i, (kind, name)
                    in enumerate(names)]
        return Observation(window_title=title, elements=elements, **kwargs)

    editor = obs("Untitled - Notepad", [("Edit", "Text Editor"), ("MenuItem", "File"),
                                        ("MenuItem", "Edit")])
    save_as = obs("Save As", [("Edit", "File name"), ("Button", "Save"),
                              ("Button", "Cancel")], dialogs=["Save As"])
    web_home = obs("Golden App", [("Link", "Sign in"), ("Button", "Search")],
                   url="https://example.test/app")
    web_error = obs("Golden App - Error", [("Text", "Something went wrong")],
                    url="https://example.test/app/error", error="HTTP 500")

    cfg = load_config(project)
    ks = cfg.make_knowledge_store()
    ids: dict[str, Any] = {}
    target_a, target_b = "notepad.exe", "https://example.test/app"
    ids["editor"] = ks.record_state(editor, target_a, "sess-1", 0)
    clock.advance(1.5)
    ids["save_as"] = ks.record_state(save_as, target_a, "sess-1", 1)
    ks.record_transition(ids["editor"], {"action": "menu", "path": "File>Save As"},
                         ids["save_as"], target_a, "sess-1")
    clock.advance(1.5)
    ks.record_transition(ids["save_as"], {"action": "key", "keys": "escape"}, ids["editor"],
                         target_a, "sess-1", success=False)
    ids["editor_again"] = ks.record_state(editor, target_a, "sess-1", 2)
    clock.advance(1.5)
    ids["bug"] = ks.record_finding("Save dialog ignores Escape", "high", ids["save_as"],
                                   [{"action": "key", "keys": "escape"}], target_a, "sess-1",
                                   expected="dialog closes", actual="dialog stays open",
                                   detail="Escape is swallowed by the file name box")
    ks.finalize_session("sess-1", target_a)
    clock.advance(60)
    ids["web_home"] = ks.record_state(web_home, target_b, "sess-2", 0)
    clock.advance(1.5)
    ids["web_error"] = ks.record_state(web_error, target_b, "sess-2", 1)
    ks.record_transition(ids["web_home"], {"action": "click", "element_id": 2}, ids["web_error"],
                         target_b, "sess-2")
    ks.record_transition(ids["web_error"], {"action": "type", "text": "a" * 30},
                         ids["web_error"], target_b, "sess-2")
    ks.record_transition(ids["web_error"], {"action": "scroll", "direction": "down"},
                         ids["web_error"], target_b, "sess-2")
    ks.record_transition(ids["web_error"], {"action": "click", "x": 5, "y": 6},
                         ids["web_home"], target_b, "sess-2")
    clock.advance(1.5)
    ks.record_assertion("text_visible", "Welcome", ids["web_error"], True, target_b, "sess-2")
    ks.record_assertion("text_visible", "Welcome", ids["web_error"], False, target_b, "sess-2")
    ks.finalize_session("sess-2", target_b)
    ks.close()

    knowledge_dir = project / ".argus" / "knowledge"
    _copy_tree(project / ".argus", out.root / "knowledge" / "project" / ".argus")

    fresh = cfg.make_knowledge_store()
    try:
        confidence_before = fresh.confidence_for_state(ids["editor"])
        stats_all = fresh.get_stats()
        stats_a = fresh.get_stats(target_a)
        stats_b = fresh.get_stats(target_b)
        context = fresh.retrieve(obs("Save As", [("Edit", "File name"), ("Button", "Save")],
                                     dialogs=["Save As"]), target_a)
        confidence_after = fresh.confidence_for_state(ids["editor"])
    finally:
        fresh.close()

    api = ArgusAPI(project_dir=project)
    export_dir = project / ".argus" / "exports"
    gui_export = api.knowledge_export(target_a)
    graph_bytes = (knowledge_dir / f"{target_key(target_a)}.graph.json").read_bytes()
    cli_export = _cli(["knowledge", "export", target_a, "--out", "exported.graph.json"],
                      project, norm)
    files = {}
    for path in sorted(knowledge_dir.iterdir()):
        files[path.name] = {"bytes": path.stat().st_size, "sha256": _sha256(path.read_bytes())}

    out.expected("knowledge", "store", "argus.knowledge.json_store.JsonKnowledgeStore", {
        "backend_info": {"type": "json",
                         "label": "JSON graph · keyword retrieval · no vector backend"},
        "files": files,
        "ids": ids,
        "get_stats_all": stats_all,
        "get_stats_target": {target_a: stats_a, target_b: stats_b},
        "confidence_for_state": {"fresh_store": confidence_before,
                                 "after_graph_loaded": confidence_after},
        "retrieve": {
            "format": context.format(),
            "past_bugs": [vars(item) for item in context.past_bugs],
            "similar_states": [vars(item) for item in context.similar_states],
            "unexplored_hints": context.unexplored_hints,
        },
        "target_key": {name: target_key(name) for name in (
            target_a, target_b, "Calc", "  ", "Ünïcode App", "x" * 50)},
    }, note="Produced with knowledge.type=json (graph only). The networkx node_link dialect "
            "written by the local/docker/external backends is not produced here.")
    out.expected("knowledge", "export_and_stats", "argus knowledge / ArgusAPI.knowledge*", {
        "argus_knowledge_stats": _cli(["knowledge", "stats"], project, norm),
        "argus_knowledge_stats_target": _cli(["knowledge", "stats", "--target", target_b],
                                             project, norm),
        "argus_knowledge_export": cli_export,
        "argus_knowledge_export_bytes_equal_graph":
            (project / "exported.graph.json").read_bytes() == graph_bytes,
        "argus_knowledge_export_missing": _cli(["knowledge", "export", "missing-app"],
                                               project, norm),
        "gui_knowledge": {
            "default": norm.value(api.knowledge()),
            target_a: norm.value(api.knowledge(target_a)),
            "example.test": norm.value(api.knowledge("example.test")),
            "unknown-app": norm.value(api.knowledge("unknown-app")),
        },
        "gui_knowledge_export": norm.value(gui_export),
        "gui_knowledge_export_bytes_equal_graph":
            (export_dir / f"{target_key(target_a)}.graph.json").read_bytes() == graph_bytes,
    }, environment={"COLUMNS": os.environ.get("COLUMNS")})


# ----------------------------------------------------------------- secrets (C-06)


REFERENCE_CASES = (
    "secret://argus/NAME",
    "secret://argus/golden/api-token",
    "secret://argus/golden-db.password_1",
    "secret://argus/a/./b",
    "secret://argus/" + "n" * 128,
    "secret://argus/" + "n" * 129,
    "SECRET://ARGUS/NAME",
    "Secret://Argus/NAME",
    "secret://argus/",
    "secret://argus/-leading-dash",
    "secret://argus/.hidden",
    "secret://argus/a/../b",
    "secret://argus/a//b",
    "secret://argus/with space",
    "secret://argus/trailing-newline\n",
    " secret://argus/leading-space",
    "secret://argus/ünïcode",
    "secret://ates/0123456789abcdef0123456789abcdef",
    "argus/NAME",
    "",
    None,
    42,
)


def _secrets(work: Path, out: _Out, det: _Determinism, env_norm: list[tuple[Path, str]],
             data_home: Path) -> None:
    import argus.secrets as secrets_module
    from argus.secrets import ArgusSecretStore, default_secret_dir, validate_secret_ref

    norm = _Normalizer(env_norm)
    clock = det.clock
    clock.set(T0 + 10_800)

    class _FrozenDatetime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return _dt.datetime.fromtimestamp(clock.now, tz or _dt.timezone.utc)

    secrets_module.datetime = _FrozenDatetime  # type: ignore[misc]

    store = ArgusSecretStore()
    store.set("secret://argus/golden/api-token", DUMMY_VALUE)
    clock.advance(60)
    store.set("secret://argus/golden-db.password", DUMMY_VALUE + "-2")
    clock.advance(60)
    store.set("secret://argus/golden/removed", DUMMY_VALUE + "-3")
    clock.advance(60)
    store.set("secret://argus/golden/api-token", DUMMY_VALUE + "-replaced")
    removed = store.delete("secret://argus/golden/removed")
    removed_again = store.delete("secret://argus/golden/removed")
    clock.advance(60)
    cwd = work / "secrets-cwd"
    cwd.mkdir()
    cli_set = _cli(["secrets", "set", "--stdin", "secret://argus/golden/from-stdin"], cwd, norm,
                   stdin=DUMMY_VALUE + "\n")
    cli_set_invalid = _cli(["secrets", "set", "--stdin", "SECRET://ARGUS/NAME"], cwd, norm,
                           stdin=DUMMY_VALUE)
    cli_list = _cli(["secrets", "list"], cwd, norm)

    root = store.root
    _copy_tree(root, out.root / "secrets" / "xdg-data" / "argus" / "secrets")

    refs = store.list_refs()
    resolved = {ref: store.get(ref) for ref in refs}

    # Permission enforcement, on copies so the exported store stays untouched.
    perm_cases = {}
    for name, filename in (("key_mode_0644", "secrets.key"),
                           ("database_mode_0644", "secrets.sqlite3")):
        copy = work / f"secrets-{name}"
        shutil.copytree(root, copy)
        os.chmod(copy / filename, 0o644)
        copied = ArgusSecretStore(copy)
        perm_cases[name] = {
            "get": _attempt(lambda c=copied: c.get("secret://argus/golden/api-token"), norm),
            "list_refs": _attempt(copied.list_refs, norm),
        }
    perm_cases["relative_root"] = _attempt(lambda: ArgusSecretStore("relative/secrets"), norm)

    locations = {}
    saved_env = os.environ.get("XDG_DATA_HOME")
    try:
        locations["XDG_DATA_HOME set"] = _attempt(default_secret_dir, norm)
        os.environ["XDG_DATA_HOME"] = ""
        locations["XDG_DATA_HOME empty"] = _attempt(default_secret_dir, norm)
        os.environ["XDG_DATA_HOME"] = "relative/data"
        locations["XDG_DATA_HOME=relative/data"] = _attempt(default_secret_dir, norm)
        del os.environ["XDG_DATA_HOME"]
        locations["XDG_DATA_HOME unset"] = _attempt(default_secret_dir, norm)
    finally:
        if saved_env is None:
            os.environ.pop("XDG_DATA_HOME", None)
        else:
            os.environ["XDG_DATA_HOME"] = saved_env

    references = []
    for ref in REFERENCE_CASES:
        outcome = _attempt(lambda ref=ref: validate_secret_ref(ref), norm)
        references.append({"accepted": "value" in outcome, "input": ref,
                           **({"error": outcome["error"]} if "error" in outcome else {})})

    dump = sqlite_dump(root / "secrets.sqlite3")
    modes = {path.name: oct(path.stat().st_mode & 0o777) for path in sorted(root.iterdir())}
    modes["."] = oct(root.stat().st_mode & 0o777)

    out.expected("secrets", "store", "argus.secrets.ArgusSecretStore", {
        "location": norm.value(root),
        "modes": modes,
        "files": sorted(path.name for path in root.iterdir()),
        "key_file": {"bytes": len(TEST_ONLY_KEY), "content_ascii": TEST_ONLY_KEY.decode("ascii"),
                     "note": "TEST ONLY: deterministic key installed by the exporter"},
        "sqlite_dump": dump,
        "payload_layout": "b'A' + nonce(12) + AES-GCM(key, nonce, utf-8 value, aad=utf-8 ref)",
        "list_refs": list(refs),
        "get_dummy_values": resolved,
        "delete": {"first": removed, "second": removed_again},
        "permission_enforcement": perm_cases,
        "default_secret_dir": locations,
    }, note="DUMMY values only. Linux path: AES-GCM with secrets.key; Windows uses DPAPI "
            "(payload prefix b'D') and cannot be produced here.")
    out.expected("secrets", "references", "argus.secrets.validate_secret_ref", references)
    out.expected("secrets", "list_command", "argus secrets list / set", {
        "secrets_list": cli_list,
        "secrets_set_stdin": cli_set,
        "secrets_set_invalid_reference": cli_set_invalid,
        "stdin_value_stored_with_trailing_newline":
            resolved.get("secret://argus/golden/from-stdin") == DUMMY_VALUE + "\n",
        "list_output_contains_values": any(
            value.strip() in cli_list["stdout"] for value in resolved.values()),
    }, note="The list command prints references only, never values (C-06).")


# ------------------------------------------------------------------- worker entry


def worker(tmp_root: Path) -> int:
    """Child-process entry point: build every section under ``<tmp_root>/out``."""
    _block_network()
    tmp_root = tmp_root.resolve()
    home = Path(os.environ.get("HOME", "")).resolve()
    if tmp_root not in home.parents:
        print("refusing to run: HOME is not redirected into the export directory",
              file=sys.stderr)
        return 2
    for name in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        if tmp_root not in Path(os.environ.get(name, "/")).resolve().parents:
            print(f"refusing to run: {name} is not redirected into the export directory",
                  file=sys.stderr)
            return 2
    if not Path("/proc/self/fd").is_dir():
        print("refusing to run: the project knowledge store needs /proc/self/fd (Linux)",
              file=sys.stderr)
        return 2
    os.chdir(tmp_root)
    det = _Determinism()
    state_home = Path(os.environ["XDG_STATE_HOME"])
    data_home = Path(os.environ["XDG_DATA_HOME"])
    env_norm = [
        (state_home, "<STATE_HOME>"),
        (data_home, "<DATA_HOME>"),
        (Path(os.environ["XDG_CONFIG_HOME"]), "<CONFIG_HOME>"),
        (home, "<HOME>"),
        (tmp_root, "<TMP>"),
        (REPO_ROOT, "<REPO>"),
    ]
    work = tmp_root / "work"
    out = _Out(tmp_root / "out")
    _history(work, out, det, env_norm)
    _tokens(work, out, env_norm)
    _conversations(work, out, env_norm, state_home)
    _knowledge(work, out, det, env_norm)
    _secrets(work, out, det, env_norm, data_home)
    return 0


# --------------------------------------------------------------------------- parent side


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                             check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def _manifest(files: dict[str, bytes]) -> dict[str, Any]:
    hashes = _source_hashes()
    try:
        from importlib.metadata import version

        package_version = version("argus-app-testing")
    except Exception:  # noqa: BLE001 - informational only
        package_version = "unknown"
    listing = {}
    for rel, data in sorted(files.items()):
        if rel.endswith(".sqlite3"):
            listing[rel] = {"compare": "sqlite-logical-dump"}
        else:
            listing[rel] = {"bytes": len(data), "sha256": _sha256(data)}
    return {
        "files": listing,
        "generated_with": {
            "_note": "informational; ignored by --check",
            "argus_package_version": package_version,
            "git_commit": _git("rev-parse", "HEAD"),
            "python": ".".join(str(part) for part in sys.version_info[:3]),
            "sources_last_commit": _git("log", "-1", "--format=%H", "--", *SOURCES),
            "sqlite": sqlite3.sqlite_version,
        },
        "generator": "scripts/golden/export_state.py",
        "implementation": {
            "name": "argus (Python)",
            "sources": hashes,
            "sources_sha256": _combined_source_hash(hashes),
        },
        "schema": SCHEMA,
        "sections": list(SECTIONS),
    }


def build_outputs() -> dict[str, bytes]:
    """Run the worker in a scrubbed environment and collect every generated file."""
    with tempfile.TemporaryDirectory(prefix="argus-golden-state-") as tmp:
        tmp_root = Path(tmp).resolve()
        env = {name: os.environ[name] for name in PASSTHROUGH_ENV if name in os.environ}
        env.update({
            "COLUMNS": "120",
            "HOME": str(tmp_root / "home"),
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(REPO_ROOT),
            "TERM": "dumb",
            "TMPDIR": str(tmp_root / "tmp"),
            "TZ": "UTC",
            "XDG_CACHE_HOME": str(tmp_root / "xdg" / "cache"),
            "XDG_CONFIG_HOME": str(tmp_root / "xdg" / "config"),
            "XDG_DATA_HOME": str(tmp_root / "xdg" / "data"),
            "XDG_STATE_HOME": str(tmp_root / "xdg" / "state"),
        })
        for key in ("home", "tmp", "xdg/cache", "xdg/config", "xdg/data", "xdg/state"):
            (tmp_root / key).mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--_worker", str(tmp_root)],
            cwd=tmp_root, env=env, capture_output=True, text=True, encoding="utf-8",
            timeout=600, check=False,
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stdout)
            sys.stderr.write(proc.stderr)
            raise SystemExit(f"golden export worker failed with exit code {proc.returncode}")
        if proc.stderr.strip():
            sys.stderr.write(proc.stderr)
        out_root = tmp_root / "out"
        files = {
            path.relative_to(out_root).as_posix(): path.read_bytes()
            for path in sorted(out_root.rglob("*")) if path.is_file()
        }
        sqlite_dumps = {rel: sqlite_dump(out_root / rel) for rel in files
                        if rel.endswith(".sqlite3")}
    files[MANIFEST_NAME] = render(_manifest(files)).encode("utf-8")
    build_outputs.sqlite_dumps = sqlite_dumps  # type: ignore[attr-defined]
    return files


def _existing() -> list[str]:
    if not GOLDEN_DIR.is_dir():
        return []
    return sorted(
        path.relative_to(GOLDEN_DIR).as_posix() for path in GOLDEN_DIR.rglob("*")
        if path.is_file() and path.relative_to(GOLDEN_DIR).as_posix() != README_NAME
    )


def _comparable_manifest(data: bytes) -> str:
    try:
        payload = json.loads(data.decode("utf-8"))
    except ValueError:
        return data.decode("utf-8", errors="replace")
    if isinstance(payload, dict):
        payload.pop("generated_with", None)
    return render(payload)


def write(outputs: dict[str, bytes]) -> int:
    for rel in _existing():
        if rel not in outputs:
            (GOLDEN_DIR / rel).unlink()
            print(f"removed stale {rel}")
    changed = 0
    for rel, data in sorted(outputs.items()):
        path = GOLDEN_DIR / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)
            changed += 1
    for directory in sorted(GOLDEN_DIR.rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    print(f"wrote {len(outputs)} files under tests/golden/state ({changed} changed)")
    return 0


def _text_diff(rel: str, have: str, want: str) -> list[str]:
    return list(difflib.unified_diff(have.splitlines(), want.splitlines(), f"committed/{rel}",
                                     f"generated/{rel}", lineterm="", n=2))


def check(outputs: dict[str, bytes]) -> int:
    dumps = getattr(build_outputs, "sqlite_dumps", {})
    problems: list[tuple[str, str, list[str]]] = []
    for rel, data in sorted(outputs.items()):
        path = GOLDEN_DIR / rel
        if not path.is_file():
            problems.append((rel, "missing committed file", []))
            continue
        current = path.read_bytes()
        if rel.endswith(".sqlite3"):
            try:
                have = render(sqlite_dump(path))
            except sqlite3.Error as exc:
                problems.append((rel, f"unreadable SQLite file: {exc}", []))
                continue
            want = render(dumps[rel])
            if have != want:
                problems.append((rel, "SQLite content differs", _text_diff(rel, have, want)))
            continue
        if rel == MANIFEST_NAME:
            have, want = _comparable_manifest(current), _comparable_manifest(data)
        elif current == data:
            continue
        else:
            have = current.decode("utf-8", errors="replace")
            want = data.decode("utf-8", errors="replace")
        if have != want or (rel != MANIFEST_NAME and current != data):
            reason = "differs" if have != want else "differs in bytes (encoding/line endings)"
            problems.append((rel, reason, _text_diff(rel, have, want)))
    for rel in _existing():
        if rel not in outputs:
            problems.append((rel, "stale committed file (not generated)", []))
    if not problems:
        print(f"OK: {len(outputs)} files under tests/golden/state match (no drift)")
        return 0
    for rel, reason, diff in problems:
        print(f"DRIFT {rel}: {reason}")
        for line in diff[:40]:
            print(f"    {line}")
        if len(diff) > 40:
            print(f"    ... {len(diff) - 40} more diff lines")
    print(f"FAILED: {len(problems)} file(s) drifted; "
          "run scripts/golden/export_state.py --write and review the diff")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate tests/golden/state")
    mode.add_argument("--check", action="store_true", help="fail when tests/golden/state drifts")
    mode.add_argument("--_worker", metavar="TMP_ROOT", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker:
        return worker(Path(args._worker))
    if not sys.platform.startswith("linux"):
        print("tests/golden/state is generated on Linux (see its README.md); "
              f"this host is {sys.platform}", file=sys.stderr)
        return 2
    outputs = build_outputs()
    return write(outputs) if args.write else check(outputs)


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.exit(main())
