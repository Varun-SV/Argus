"""Export golden ATES evidence stores for compatibility contract C-03.

Specification C-03: "evidence written by the Python implementation MUST verify under the
Rust implementation [...]. This is checked with golden stores exported from Python
(``tests/golden/ates/``)." This script produces those stores with the real writer, a real
``argus run`` of small CLI test specs, and records what the real Python verifiers say about
each store, untouched and after deliberate tampering:

* ``stores/<case>/project/.argus/runs/``  the ATES run directory (``evidence.jsonl``,
  ``audit.jsonl``, ``approvals.jsonl``, ``run.json``, ``manifests/``, ``reports/``) and the
  project's ``.ates-runtime-identity.key``, which keys the HMAC commitments in the evidence.
  The key is **TEST ONLY** material derived from a fixed seed (see Determinism);
* ``stores/<case>/project/.argus/<case>.test.yaml``  the spec that was run;
* ``expected/<case>.json``  ``inspect_finalization_trust``, ``verify_finalized_run`` and
  ``verify_report_bundle`` on the untouched store, the ``argus run`` exit code, the event
  types in order and the effective status;
* ``expected/tamper.json``  the same verifiers on copies of the ``cli-pass`` store with one
  file changed, removed or truncated. The Rust verifier must reject every one of them.

Lock files (``*.lock``) are not exported; the writer creates them on demand. Legacy run
history (``<stamp>-<spec>.json`` and its directory) is C-02 and lives in
``tests/golden/state``.

Usage::

    python scripts/golden/export_ates.py --write   # regenerate tests/golden/ates
    python scripts/golden/export_ates.py --check   # exit 1 and name every drifting file

Determinism: the writer runs in a child process with a scrubbed environment (as in
``export_state.py``). The worker imports every ``argus`` module first and then installs
documented monkeypatches: ``datetime.datetime`` in every ``argus.*`` module is replaced by a
subclass whose ``now()`` returns a clock that starts at 2026-01-01T00:00:00Z and advances 1 ms
per call; ``time.time``/``time.time_ns``/``time.monotonic``/``time.perf_counter`` advance in
the same fixed steps; ``uuid.uuid4``, ``secrets.token_bytes``/``token_hex`` and
``os.urandom`` return SHA-256 counters over a fixed label; and ``argus.__version__`` (as
recorded in ``RUN_STARTED``) is pinned to ``0.0.0+golden``. The CLI targets run
``python3 -c ...`` from ``PATH``; the spec text, and so its HMAC source commitment, contains
no host path.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.golden.export_state import (  # noqa: E402 - path set up above
    CONSOLE_WIDTH,
    PASSTHROUGH_ENV,
    _block_network,
    _comparable_manifest,
    _git,
    _sha256,
    _text_diff,
    render,
)

SCHEMA = "argus-golden-ates-v1"
GOLDEN_DIR = REPO_ROOT / "tests" / "golden" / "ates"
MANIFEST_NAME = "_manifest.json"
README_NAME = "README.md"
# Hand-written files that live next to the generated ones.
UNGENERATED = (README_NAME, ".gitattributes")
PINNED_VERSION = "0.0.0+golden"
CLOCK_START = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)

# Python sources whose behaviour the generated files describe.
SOURCES = (
    "argus/adapters/cli_adapter.py",
    "argus/ates/artifacts.py",
    "argus/ates/audit.py",
    "argus/ates/core.py",
    "argus/ates/evidence_validation.py",
    "argus/ates/finalization.py",
    "argus/ates/finalization_io.py",
    "argus/ates/package.py",
    "argus/ates/privacy.py",
    "argus/ates/reports.py",
    "argus/ates/status.py",
    "argus/ates/store.py",
    "argus/engine/ates_runtime.py",
    "argus/engine/runner_impl.py",
)

# One CLI spec per case.
CASES: dict[str, dict[str, Any]] = {
    "cli-pass": {
        "note": "two deterministic assertions that pass",
        "spec": (
            "name: CLI passes\n"
            "target:\n"
            "  adapter: cli\n"
            "  launch: 'python3 -c \"print(''OK'')\"'\n"
            "steps:\n"
            "  - assert:\n"
            "      exit_code_is: 0\n"
            "  - assert:\n"
            "      stdout_contains: \"OK\"\n"
        ),
    },
    "cli-fail": {
        "note": "the command exits 3 and writes to stderr; one assertion fails",
        "spec": (
            "name: CLI fails\n"
            "target:\n"
            "  adapter: cli\n"
            "  launch: 'python3 -c \"import sys; sys.stderr.write(''boom''); sys.exit(3)\"'\n"
            "steps:\n"
            "  - assert:\n"
            "      stderr_contains: \"boom\"\n"
            "  - assert:\n"
            "      exit_code_is: 0\n"
        ),
    },
    "cli-launch-error": {
        "note": "the target program does not exist",
        "spec": (
            "name: CLI cannot launch\n"
            "target:\n"
            "  adapter: cli\n"
            "  launch: \"argus-golden-no-such-program --flag\"\n"
            "steps:\n"
            "  - assert:\n"
            "      exit_code_is: 0\n"
        ),
    },
}


# --------------------------------------------------------------------------- worker side


class _Determinism:
    """Documented monkeypatches that make one ``argus run`` reproducible."""

    def __init__(self) -> None:
        import pkgutil
        import secrets as _secrets
        import time as _time
        import uuid as _uuid

        import argus

        # Import every argus module first, so that modules the run would import lazily are
        # patched too. Optional extras that are not installed are skipped.
        for info in pkgutil.walk_packages(argus.__path__, "argus."):
            if info.name.startswith(("argus.gui", "argus.serve")):
                continue
            try:
                __import__(info.name)
            except Exception:  # noqa: BLE001 - optional dependencies
                pass

        self._ticks = 0
        self._counters: dict[str, int] = {}
        clock = self

        class _FrozenDatetime(_dt.datetime):
            # Return instances of this class: argus modules check
            # ``isinstance(value, datetime)`` against the (replaced) module-level name.
            @classmethod
            def _make(cls, value: _dt.datetime) -> "_dt.datetime":
                return cls(value.year, value.month, value.day, value.hour, value.minute,
                           value.second, value.microsecond, tzinfo=value.tzinfo)

            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                value = clock._next_datetime()
                return cls._make(value.astimezone(tz) if tz is not None
                                 else value.replace(tzinfo=None))

            @classmethod
            def utcnow(cls):  # type: ignore[override]
                return cls._make(clock._next_datetime().replace(tzinfo=None))

        for name, module in list(sys.modules.items()):
            if not name.startswith("argus") or module is None:
                continue
            if getattr(module, "datetime", None) is _dt.datetime:
                module.datetime = _FrozenDatetime  # type: ignore[attr-defined]
            if getattr(module, "__version__", None) == argus.__version__:
                module.__version__ = PINNED_VERSION  # type: ignore[attr-defined]
        _time.time = self._seconds  # type: ignore[assignment]
        _time.time_ns = self._nanoseconds  # type: ignore[assignment]
        _time.monotonic = self._seconds  # type: ignore[assignment]
        _time.perf_counter = self._seconds  # type: ignore[assignment]
        _uuid.uuid4 = self._uuid4  # type: ignore[assignment]
        _secrets.token_bytes = self._bytes  # type: ignore[assignment]
        _secrets.token_hex = lambda n=32: self._bytes(n).hex()  # type: ignore[assignment]
        os.urandom = self._bytes  # type: ignore[assignment]

    def _tick(self) -> int:
        self._ticks += 1
        return self._ticks

    def _next_datetime(self) -> _dt.datetime:
        return CLOCK_START + _dt.timedelta(milliseconds=self._tick())

    def _seconds(self) -> float:
        return CLOCK_START.timestamp() + self._tick() / 1000

    def _nanoseconds(self) -> int:
        return int(CLOCK_START.timestamp()) * 1_000_000_000 + self._tick() * 1_000_000

    def _derive(self, label: str, n: int) -> bytes:
        index = self._counters.get(label, 0) + 1
        self._counters[label] = index
        out, block = b"", 0
        while len(out) < n:
            out += hashlib.sha256(f"argus-golden-ates-{label}-{index}-{block}".encode()).digest()
            block += 1
        return out[:n]

    def _uuid4(self):
        import uuid as _uuid

        return _uuid.UUID(bytes=self._derive("uuid", 16), version=4)

    def _bytes(self, n: int | None = None) -> bytes:
        return self._derive("bytes", 32 if n is None else n)


def _plain(value: Any) -> Any:
    """JSON-compatible view of verifier results (dataclasses, enums, paths)."""
    from dataclasses import fields, is_dataclass
    from enum import Enum

    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _plain(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, frozenset, set)):
        items = [_plain(v) for v in value]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def _verify(run_dir: Path, project: Path) -> dict[str, Any]:
    from argus.ates import inspect_finalization_trust, verify_finalized_run, verify_report_bundle

    def attempt(fn):
        try:
            result = _plain(fn(run_dir))
        except Exception as exc:  # noqa: BLE001 - rejections are part of the contract
            return {"error": {"message": str(exc).replace(str(project), "<PROJECT>"),
                              "type": type(exc).__name__}}
        text = json.dumps(result).replace(str(project), "<PROJECT>")
        return {"value": json.loads(text)}

    return {
        "inspect_finalization_trust": attempt(inspect_finalization_trust),
        "verify_finalized_run": attempt(verify_finalized_run),
        "verify_report_bundle": attempt(verify_report_bundle),
    }


def _export_files(project: Path, dest: Path) -> list[str]:
    """Copy the ATES store of ``project`` (no locks, no legacy history) into ``dest``."""
    runs = project / ".argus" / "runs"
    copied = []
    for path in sorted(runs.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.name.endswith(".lock"):
            continue
        rel = path.relative_to(project)
        top = path.relative_to(runs).parts[0]
        if not (top.startswith("RUN-") or top == ".ates-runtime-identity.key"):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copied.append(rel.as_posix())
    for spec in sorted((project / ".argus").glob("*.test.yaml")):
        target = dest / spec.relative_to(project)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(spec, target)
    return copied


def _tampered(source: Path, work: Path, project_label: str) -> list[dict[str, Any]]:
    """Verifier results on copies of ``source`` with one member changed."""

    def run_dir_of(project: Path) -> Path:
        return next((project / ".argus" / "runs").glob("RUN-*"))

    def flip_last_byte(path: Path) -> None:
        data = bytearray(path.read_bytes())
        data[-2] ^= 0x01  # the last byte is a newline in every member
        path.write_bytes(bytes(data))

    def alter_first_digest(path: Path) -> None:
        # Change one hex digit of the first "sha256:<hex>" value; the JSON stays valid.
        data = bytearray(path.read_bytes())
        start = data.index(b"sha256:") + len(b"sha256:")
        data[start] = ord("0") if data[start] != ord("0") else ord("1")
        path.write_bytes(bytes(data))

    def truncate_last_line(path: Path) -> None:
        lines = path.read_bytes().splitlines(keepends=True)
        path.write_bytes(b"".join(lines[:-1]))

    mutations = [
        ("evidence-byte-flipped", "evidence.jsonl", flip_last_byte),
        ("evidence-last-event-removed", "evidence.jsonl", truncate_last_line),
        ("evidence-manifest-byte-flipped", "manifests/manifest-0001.json", flip_last_byte),
        ("evidence-manifest-digest-altered", "manifests/manifest-0001.json", alter_first_digest),
        ("package-manifest-byte-flipped", "manifests/package-manifest-0001.json", flip_last_byte),
        ("package-manifest-digest-altered", "manifests/package-manifest-0001.json",
         alter_first_digest),
        ("run-binding-byte-flipped", "run.json", flip_last_byte),
        ("run-binding-digest-altered", "run.json", alter_first_digest),
        ("audit-byte-flipped", "audit.jsonl", flip_last_byte),
        ("report-md-byte-flipped", "reports/report.md", flip_last_byte),
        ("report-manifest-byte-flipped", "reports/report-manifest-0001.json", flip_last_byte),
        ("report-manifest-digest-altered", "reports/report-manifest-0001.json",
         alter_first_digest),
        ("report-json-removed", "reports/report.json", lambda p: p.unlink()),
    ]
    results = []
    for name, member, mutate in mutations:
        project = work / f"tamper-{name}" / "project"
        shutil.copytree(source, project)
        run_dir = run_dir_of(project)
        mutate(run_dir / member)
        results.append({
            "case": name,
            "member": member,
            "source": project_label,
            "verifiers": _verify(run_dir, project),
        })
    return results


def worker(tmp_root: Path) -> int:
    """Child-process entry point: build every case under ``<tmp_root>/out``."""
    _block_network()
    tmp_root = tmp_root.resolve()
    home = Path(os.environ.get("HOME", "")).resolve()
    if tmp_root not in home.parents:
        print("refusing to run: HOME is not redirected into the export directory",
              file=sys.stderr)
        return 2
    _Determinism()
    from click.testing import CliRunner

    from argus.cli import main as argus_main

    out = tmp_root / "out"
    work = tmp_root / "work"
    for case, info in CASES.items():
        project = work / case / "project"
        (project / ".argus").mkdir(parents=True)
        (project / ".argus" / "config.yaml").write_text("provider: ollama\n", encoding="utf-8")
        spec_name = f"{case}.test.yaml"
        (project / ".argus" / spec_name).write_text(info["spec"], encoding="utf-8")
        previous = Path.cwd()
        os.chdir(project)
        try:
            result = CliRunner().invoke(argus_main, ["run", spec_name], catch_exceptions=True)
        finally:
            os.chdir(previous)
        run_dirs = sorted((project / ".argus" / "runs").glob("RUN-*"))
        if len(run_dirs) != 1:
            print(f"{case}: expected one ATES run directory, found {len(run_dirs)}",
                  file=sys.stderr)
            print(result.output, file=sys.stderr)
            return 1
        run_dir = run_dirs[0]
        store = out / "stores" / case / "project"
        files = _export_files(project, store)
        # The committed copy must verify on its own (no locks, no legacy history). Verify a
        # scratch copy: verification takes locks, and lock files are not exported.
        scratch = work / f"{case}-exported" / "project"
        shutil.copytree(store, scratch)
        exported = _verify(scratch / run_dir.relative_to(project), scratch)
        live = _verify(run_dir, project)
        if exported != live:
            print(f"{case}: exported store verifies differently from the live store",
                  file=sys.stderr)
            return 1
        events = [json.loads(line) for line in
                  (run_dir / "evidence.jsonl").read_text(encoding="utf-8").splitlines()]
        binding = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        expected = {
            "api": "argus run <spec>; argus.ates.inspect_finalization_trust / "
                   "verify_finalized_run / verify_report_bundle",
            "case": case,
            "note": info["note"],
            "schema": SCHEMA,
            "value": {
                "argus_run_exit_code": result.exit_code,
                "effective_status": binding.get("effective_status"),
                "event_types": [event["event_type"] for event in events],
                "files": files,
                "run_dir": run_dir.relative_to(project).as_posix(),
                "trust_state": binding.get("trust_state"),
                "verifiers": exported,
            },
        }
        path = out / "expected" / f"{case}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(expected), encoding="utf-8")
    tamper = {
        "api": "argus.ates verifiers on modified copies of stores/cli-pass",
        "case": "tamper",
        "note": "every case must be rejected by both implementations",
        "schema": SCHEMA,
        "value": _tampered(out / "stores" / "cli-pass" / "project", work, "cli-pass"),
    }
    accepted = [item["case"] for item in tamper["value"]
                if all("value" in v and not (isinstance(v["value"], dict)
                                             and v["value"].get("error"))
                       for v in item["verifiers"].values())]
    if accepted:
        print(f"tampered stores were accepted by every verifier: {accepted}", file=sys.stderr)
        return 1
    (out / "expected" / "tamper.json").write_text(render(tamper), encoding="utf-8")
    return 0


# --------------------------------------------------------------------------- parent side


def _source_hashes() -> dict[str, str]:
    return {rel: _sha256((REPO_ROOT / rel).read_bytes().replace(b"\r\n", b"\n"))
            for rel in SOURCES}


def _manifest(files: dict[str, bytes]) -> dict[str, Any]:
    hashes = _source_hashes()
    combined = _sha256("".join(f"{rel}\0{hashes[rel]}\n" for rel in sorted(hashes)).encode())
    try:
        from importlib.metadata import version

        package_version = version("argus-app-testing")
    except Exception:  # noqa: BLE001 - informational only
        package_version = "unknown"
    return {
        "cases": list(CASES),
        "files": {rel: {"bytes": len(data), "sha256": _sha256(data)}
                  for rel, data in sorted(files.items())},
        "generated_with": {
            "_note": "informational; ignored by --check",
            "argus_package_version": package_version,
            "git_commit": _git("rev-parse", "HEAD"),
            "python": ".".join(str(part) for part in sys.version_info[:3]),
            "sources_last_commit": _git("log", "-1", "--format=%H", "--", *SOURCES),
        },
        "generator": "scripts/golden/export_ates.py",
        "implementation": {
            "name": "argus (Python)",
            "pinned_version": PINNED_VERSION,
            "sources": hashes,
            "sources_sha256": combined,
        },
        "schema": SCHEMA,
    }


def build_outputs() -> dict[str, bytes]:
    with tempfile.TemporaryDirectory(prefix="argus-golden-ates-") as tmp:
        tmp_root = Path(tmp).resolve()
        env = {name: os.environ[name] for name in PASSTHROUGH_ENV if name in os.environ}
        env.update({
            "COLUMNS": CONSOLE_WIDTH,
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
        out_root = tmp_root / "out"
        files = {path.relative_to(out_root).as_posix(): path.read_bytes()
                 for path in sorted(out_root.rglob("*")) if path.is_file()}
    files[MANIFEST_NAME] = render(_manifest(files)).encode("utf-8")
    return files


def _existing() -> list[str]:
    if not GOLDEN_DIR.is_dir():
        return []
    return sorted(path.relative_to(GOLDEN_DIR).as_posix() for path in GOLDEN_DIR.rglob("*")
                  if path.is_file()
                  and path.relative_to(GOLDEN_DIR).as_posix() not in UNGENERATED)


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
    print(f"wrote {len(outputs)} files under tests/golden/ates ({changed} changed)")
    return 0


def check(outputs: dict[str, bytes]) -> int:
    problems: list[tuple[str, str, list[str]]] = []
    for rel, data in sorted(outputs.items()):
        path = GOLDEN_DIR / rel
        if not path.is_file():
            problems.append((rel, "missing committed file", []))
            continue
        current = path.read_bytes()
        if rel == MANIFEST_NAME:
            have, want = _comparable_manifest(current), _comparable_manifest(data)
            if have != want:
                problems.append((rel, "differs", _text_diff(rel, have, want)))
        elif current != data:
            problems.append((rel, "differs", _text_diff(
                rel, current.decode("utf-8", "replace"), data.decode("utf-8", "replace"))))
    for rel in _existing():
        if rel not in outputs:
            problems.append((rel, "stale committed file (not generated)", []))
    if not problems:
        print(f"OK: {len(outputs)} files under tests/golden/ates match (no drift)")
        return 0
    for rel, reason, diff in problems:
        print(f"DRIFT {rel}: {reason}")
        for line in diff[:40]:
            print(f"    {line}")
    print(f"FAILED: {len(problems)} file(s) drifted; "
          "run scripts/golden/export_ates.py --write and review the diff")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate tests/golden/ates")
    mode.add_argument("--check", action="store_true", help="fail when tests/golden/ates drifts")
    mode.add_argument("--_worker", metavar="TMP_ROOT", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker:
        return worker(Path(args._worker))
    if not sys.platform.startswith("linux"):
        print("tests/golden/ates is generated on Linux (see its README.md); "
              f"this host is {sys.platform}", file=sys.stderr)
        return 2
    outputs = build_outputs()
    return write(outputs) if args.write else check(outputs)


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.exit(main())
