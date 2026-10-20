"""Export golden fixtures for compatibility contract C-01 (configuration and test specs).

The real Python loaders interpret every committed case under
``tests/golden/config/cases`` and the interpretation is recorded under
``tests/golden/config/expected``:

* ``cases/config/<name>.yaml``  -> ``argus.config.load_config`` (copied to
  ``<PROJECT>/.argus/config.yaml``; an optional ``<name>.load.json`` sidecar sets
  test-only environment variables, the session ``provider`` argument, or marks
  the config file as absent);
* ``cases/spec/<name>.test.yaml`` -> ``argus.engine.spec.load_spec`` (copied to
  ``<PROJECT>/.argus/<name>.test.yaml``);
* ``cases/programmatic/<name>.json`` -> the ``TestSpec`` / ``NLStep`` /
  ``AssertStep`` constructors (strict validation of programmatic step metadata).

Usage::

    python scripts/golden/export_config.py --write   # regenerate expected/ deterministically
    python scripts/golden/export_config.py --check   # exit 1 and name every drifting case

The loaders run in a child process with a scrubbed environment: ``HOME`` and the
XDG directories point into a fresh temporary directory, no ``ARGUS_*`` or provider
variables are inherited, socket connections and name lookups are refused, and no
provider object is ever constructed. Absolute paths are replaced by ``<PROJECT>``,
``<HOME>`` and ``<TMP>``. Secret references are recorded as written; nothing is
resolved from a secret store.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import datetime as _dt
import difflib
import hashlib
import io
import json
import logging
import math
import os
import shutil
import subprocess
import sys
import tempfile
import warnings
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

SCHEMA = "argus-golden-config-v1"
REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "tests" / "golden" / "config"
CASES_DIR = GOLDEN_DIR / "cases"
EXPECTED_DIR = GOLDEN_DIR / "expected"
MANIFEST_NAME = "_manifest.json"
CATEGORIES = ("config", "spec", "programmatic")

# Python sources whose behaviour the expected files describe. Their hashes are
# recorded so a change to a loader shows up as drift in every expected file.
LOADER_SOURCES = (
    "argus/adapters/base.py",
    "argus/capsule/base.py",
    "argus/capsule/files.py",
    "argus/config.py",
    "argus/engine/spec.py",
    "argus/secrets.py",
    "argus/tokens.py",
)

SIDECAR_SUFFIX = ".load.json"
SIDECAR_KEYS = frozenset({"env", "provider", "config_file"})
PROGRAMMATIC_KEYS = frozenset({"description", "construct", "args"})

# Variables copied into the worker's environment. Everything else (ARGUS_*,
# provider API keys, proxies, real HOME/XDG locations) is deliberately dropped.
PASSTHROUGH_ENV = ("PATH", "SYSTEMROOT", "VIRTUAL_ENV")


# --------------------------------------------------------------------------- shared helpers


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _source_hashes() -> dict[str, str]:
    hashes = {}
    for rel in LOADER_SOURCES:
        data = (REPO_ROOT / rel).read_bytes().replace(b"\r\n", b"\n")
        hashes[rel] = _sha256(data)
    return hashes


def _combined_source_hash(hashes: dict[str, str]) -> str:
    text = "".join(f"{rel}\0{hashes[rel]}\n" for rel in sorted(hashes))
    return _sha256(text.encode("utf-8"))


@dataclasses.dataclass(frozen=True)
class Case:
    category: str
    name: str
    input_path: Path | None
    sidecar_path: Path | None = None

    @property
    def case_id(self) -> str:
        return f"{self.category}/{self.name}"

    @property
    def expected_path(self) -> Path:
        return EXPECTED_DIR / self.category / f"{self.name}.json"


def discover_cases() -> list[Case]:
    """Every committed case, in a stable order. Unknown files fail loudly."""
    cases: list[Case] = []
    config_dir = CASES_DIR / "config"
    inputs: dict[str, Path] = {}
    sidecars: dict[str, Path] = {}
    for path in sorted(config_dir.iterdir()):
        if path.name.endswith(SIDECAR_SUFFIX):
            sidecars[path.name[: -len(SIDECAR_SUFFIX)]] = path
        elif path.name.endswith(".yaml"):
            inputs[path.name[: -len(".yaml")]] = path
        else:
            raise SystemExit(f"unexpected file in {config_dir}: {path.name}")
    for name in sorted(set(inputs) | set(sidecars)):
        cases.append(Case("config", name, inputs.get(name), sidecars.get(name)))

    for path in sorted((CASES_DIR / "spec").iterdir()):
        if not path.name.endswith(".test.yaml"):
            raise SystemExit(f"spec cases must be named <case>.test.yaml: {path.name}")
        cases.append(Case("spec", path.name[: -len(".test.yaml")], path))

    for path in sorted((CASES_DIR / "programmatic").iterdir()):
        if path.suffix != ".json":
            raise SystemExit(f"programmatic cases must be JSON files: {path.name}")
        cases.append(Case("programmatic", path.stem, path))
    return cases


def render(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------- worker side


class _Normalizer:
    """Turns loader results into stable, path-free JSON values."""

    def __init__(self, replacements: list[tuple[str, str]]) -> None:
        unique = {}
        for needle, token in replacements:
            if needle and needle not in unique:
                unique[needle] = token
        self._replacements = sorted(unique.items(), key=lambda item: -len(item[0]))

    def text(self, value: str) -> str:
        for needle, token in self._replacements:
            value = value.replace(needle, token)
        return value

    def value(self, value: Any) -> Any:
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float):
            if math.isnan(value):
                return {"$float": "nan"}
            if math.isinf(value):
                return {"$float": "inf" if value > 0 else "-inf"}
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, Path):
            return self.text(value.as_posix())
        if isinstance(value, dict):
            if all(isinstance(key, str) for key in value):
                return {self.text(key): self.value(item) for key, item in value.items()}
            return {"$mapping": [[self.value(k), self.value(v)] for k, v in value.items()]}
        if isinstance(value, (list, tuple)):
            return [self.value(item) for item in value]
        if isinstance(value, (set, frozenset)):
            items = [self.value(item) for item in value]
            return {"$set": sorted(items, key=lambda item: json.dumps(item, sort_keys=True))}
        if isinstance(value, _dt.datetime):
            return {"$datetime": value.isoformat()}
        if isinstance(value, _dt.date):
            return {"$date": value.isoformat()}
        if isinstance(value, bytes):
            return {"$bytes_base64": base64.b64encode(value).decode("ascii")}
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return self.value(dataclasses.asdict(value))
        return {"$repr": self.text(repr(value)), "$type": type(value).__name__}


def _error_category(exc: BaseException) -> str:
    import yaml

    from argus.capsule.base import CapsuleError
    from argus.engine.spec import SpecError
    from argus.secrets import SecretStoreError

    for cls, name in (
        (SpecError, "spec_error"),
        (yaml.YAMLError, "yaml_error"),
        (CapsuleError, "capsule_error"),
        (SecretStoreError, "secret_reference_error"),
        (ValueError, "value_error"),
        (TypeError, "type_error"),
        (AttributeError, "attribute_error"),
        (KeyError, "key_error"),
    ):
        if isinstance(exc, cls):
            return name
    return "other_error"


def _describe_error(exc: BaseException, norm: _Normalizer) -> dict[str, Any]:
    record: dict[str, Any] = {
        "category": _error_category(exc),
        "message": norm.text(str(exc)),
        "module": type(exc).__module__,
        "type": type(exc).__name__,
    }
    cause = exc.__cause__
    if cause is not None:
        record["cause"] = {
            "message": norm.text(str(cause)),
            "module": type(cause).__module__,
            "type": type(cause).__name__,
        }
    return record


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def _invoke(fn: Callable[[], Any], norm: _Normalizer) -> tuple[Any, BaseException | None, dict]:
    """Run ``fn`` and capture its result, exception, warnings, logs and output."""
    root = logging.getLogger()
    handler = _LogCapture()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    out, err = io.StringIO(), io.StringIO()
    result: Any = None
    error: BaseException | None = None
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with redirect_stdout(out), redirect_stderr(err):
                try:
                    result = fn()
                except Exception as exc:  # noqa: BLE001 - every failure is part of the contract
                    error = exc
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
    diagnostics = {
        "log_records": [
            {
                "level": record.levelname,
                "logger": record.name,
                "message": norm.text(record.getMessage()),
            }
            for record in handler.records
        ],
        "stderr": norm.text(err.getvalue()),
        "stdout": norm.text(out.getvalue()),
        "warnings": [
            {"category": w.category.__name__, "message": norm.text(str(w.message))}
            for w in caught
        ],
    }
    return result, error, diagnostics


def _attempt(fn: Callable[[], Any], norm: _Normalizer) -> dict[str, Any]:
    try:
        return {"value": norm.value(fn())}
    except Exception as exc:  # noqa: BLE001 - derived failures are recorded, not raised
        return {"error": _describe_error(exc, norm)}


def _secret_like_strings(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            found.extend(_secret_like_strings(item, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(_secret_like_strings(item, f"{prefix}[{index}]"))
    elif isinstance(value, str) and value.strip().lower().startswith("secret://"):
        found.append((prefix, value))
    return found


def _config_value(cfg: Any, norm: _Normalizer) -> dict[str, Any]:
    from argus.secrets import validate_secret_ref
    from argus.tokens import TokenTracker

    def budget() -> dict[str, Any]:
        made = cfg.make_budget(TokenTracker())
        return {"max_seconds": made.max_seconds, "max_tokens": made.max_tokens}

    secret_refs = []
    for where, ref in _secret_like_strings(cfg.raw):
        check = _attempt(lambda ref=ref: validate_secret_ref(ref), norm)
        secret_refs.append({
            "path": where,
            "reference": ref,
            "validate_secret_ref": (
                "accepted" if "value" in check else check["error"]["message"]
            ),
        })

    return norm.value({
        "argus_dir": cfg.argus_dir,
        "budgets": {"max_tokens": cfg.max_tokens, "time_minutes": cfg.time_minutes},
        "derived": {
            "budget": _attempt(budget, norm),
            "knowledge_persist_dir": _attempt(cfg.knowledge_persist_dir, norm),
            "secret_references": secret_refs,
        },
        "execution": dataclasses.asdict(cfg.execution),
        "knowledge": dataclasses.asdict(cfg.knowledge),
        "project_dir": cfg.project_dir,
        "provider": dataclasses.asdict(cfg.provider),
        "raw": cfg.raw,
    })


def _step_value(step: Any, norm: _Normalizer) -> dict[str, Any]:
    from argus.engine.spec import AssertStep, NLStep

    if isinstance(step, NLStep):
        return {"class": "NLStep", "kind": step.kind, "text": norm.value(step.text)}
    if isinstance(step, AssertStep):
        return {
            "assertion": step.assertion,
            "class": "AssertStep",
            "describe": norm.text(step.describe()),
            "expected": norm.value(step.expected),
            "kind": step.kind,
        }
    return {"class": type(step).__name__, "value": norm.value(step)}


def _spec_value(spec: Any, norm: _Normalizer) -> dict[str, Any]:
    return {
        "adapter": norm.value(spec.adapter),
        "collect": norm.value(spec.collect),
        "continue_on_failure": spec.continue_on_failure,
        "file_name": norm.value(spec.file_name),
        "launch": norm.value(spec.launch),
        "name": norm.value(spec.name),
        "path": norm.value(spec.path),
        "retries": spec.retries,
        "staging": norm.value([dataclasses.asdict(item) for item in spec.staging]),
        "steps": [_step_value(step, norm) for step in spec.steps],
    }


def _load_json_object(path: Path, allowed: frozenset) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: must contain a JSON object")
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise SystemExit(f"{path}: unknown key(s): {', '.join(unknown)}")
    return data


def _fresh_project(tmp_root: Path) -> Path:
    work = tmp_root / "work"
    if work.exists():
        shutil.rmtree(work)
    project = work / "project"
    (project / ".argus").mkdir(parents=True)
    return project


def _run_config_case(case: Case, tmp_root: Path, norm_for: Callable[[Path], _Normalizer]):
    from argus.config import load_config

    sidecar: dict[str, Any] = {}
    if case.sidecar_path is not None:
        sidecar = _load_json_object(case.sidecar_path, SIDECAR_KEYS)
    config_file = sidecar.get("config_file", "present")
    if config_file not in {"present", "absent"}:
        raise SystemExit(f"{case.case_id}: config_file must be 'present' or 'absent'")
    if (config_file == "absent") != (case.input_path is None):
        raise SystemExit(f"{case.case_id}: config_file disagrees with the case files")
    env = sidecar.get("env") or {}
    if not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise SystemExit(f"{case.case_id}: env must map names to strings")
    provider = sidecar.get("provider")

    project = _fresh_project(tmp_root)
    if case.input_path is not None:
        shutil.copyfile(case.input_path, project / ".argus" / "config.yaml")
    norm = norm_for(project)

    saved = {name: os.environ.get(name) for name in env}
    os.environ.update(env)
    try:
        cfg, error, diagnostics = _invoke(lambda: load_config(project, provider=provider), norm)
        value = None if error is not None else _config_value(cfg, norm)
    finally:
        for name, old in saved.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old
    return (
        "argus.config.load_config",
        {"env": dict(sorted(env.items())), "provider_argument": provider,
         "config_file": config_file},
        value, error, diagnostics, norm,
    )


def _run_spec_case(case: Case, tmp_root: Path, norm_for: Callable[[Path], _Normalizer]):
    from argus.engine.spec import load_spec

    project = _fresh_project(tmp_root)
    target = project / ".argus" / case.input_path.name
    shutil.copyfile(case.input_path, target)
    norm = norm_for(project)
    spec, error, diagnostics = _invoke(lambda: load_spec(target), norm)
    value = None if error is not None else _spec_value(spec, norm)
    return "argus.engine.spec.load_spec", {}, value, error, diagnostics, norm


def _run_programmatic_case(case: Case, tmp_root: Path, norm_for: Callable[[Path], _Normalizer]):
    from argus.engine.spec import AssertStep, NLStep, StageFile, TestSpec

    data = _load_json_object(case.input_path, PROGRAMMATIC_KEYS)
    construct = data.get("construct")
    args = dict(data.get("args") or {})
    classes = {"NLStep": NLStep, "AssertStep": AssertStep}

    def build_step(desc: dict[str, Any]) -> Any:
        if desc.get("class") == "raw":
            return desc["value"]
        obj = classes[desc["class"]](**desc.get("args", {}))
        for attr, new in desc.get("mutate", {}).items():
            setattr(obj, attr, new)
        return obj

    def run() -> Any:
        if construct in classes:
            return build_step({"class": construct, "args": args})
        if construct != "TestSpec":
            raise SystemExit(f"{case.case_id}: unknown construct {construct!r}")
        kwargs = dict(args)
        kwargs["steps"] = [build_step(desc) for desc in args.get("steps", [])]
        if "staging" in args:
            kwargs["staging"] = [StageFile(**item) for item in args["staging"]]
        return TestSpec(**kwargs)

    norm = norm_for(_fresh_project(tmp_root))
    result, error, diagnostics = _invoke(run, norm)
    value = None
    if error is None:
        value = _step_value(result, norm) if construct in classes else _spec_value(result, norm)
    loader = f"argus.engine.spec.{construct}"
    return loader, {}, value, error, diagnostics, norm


RUNNERS = {
    "config": _run_config_case,
    "spec": _run_spec_case,
    "programmatic": _run_programmatic_case,
}


def _block_network() -> None:
    import socket

    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("network access is disabled during golden export")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.getaddrinfo = refuse  # type: ignore[assignment]


def worker(tmp_root: Path) -> int:
    """Child-process entry point: print every case's interpretation as JSON."""
    _block_network()
    tmp_root = tmp_root.resolve()
    home = Path(os.environ.get("HOME", "")).resolve()
    if tmp_root not in home.parents:
        print("refusing to run: HOME is not redirected into the export directory",
              file=sys.stderr)
        return 2
    os.chdir(tmp_root)
    if not (os.name == "nt" or Path("/proc/self/fd").is_dir()):
        print("refusing to run: the knowledge.type default differs on this platform "
              "(argus/config.py uses 'json' without /proc); generate on Linux",
              file=sys.stderr)
        return 2

    combined = _combined_source_hash(_source_hashes())

    def norm_for(project: Path) -> _Normalizer:
        pairs = []
        for path, token in ((project, "<PROJECT>"), (home, "<HOME>"), (tmp_root, "<TMP>"),
                            (REPO_ROOT, "<REPO>")):
            pairs.append((str(path.resolve()), token))
            pairs.append((str(path), token))
        return _Normalizer(pairs)

    results = {}
    for case in discover_cases():
        loader, load, value, error, diagnostics, norm = RUNNERS[case.category](
            case, tmp_root, norm_for)
        inputs = [case.input_path, case.sidecar_path]
        results[case.case_id] = {
            "case": case.case_id,
            "category": case.category,
            "diagnostics": diagnostics,
            "error": None if error is None else _describe_error(error, norm),
            "implementation": {
                "name": "argus (Python)",
                "loader_sources_sha256": combined,
            },
            "input": {
                "files": [
                    {"path": path.relative_to(GOLDEN_DIR).as_posix(),
                     "sha256": _sha256(path.read_bytes())}
                    for path in inputs if path is not None
                ],
                **load,
            },
            "loader": loader,
            "outcome": "error" if error is not None else "loaded",
            "schema": SCHEMA,
            "value": value,
        }
    sys.stdout.write(json.dumps(results, sort_keys=True, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------- parent side


def _git(*args: str) -> str:
    try:
        out = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                             check=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    return out.stdout.strip() or "unknown"


def _run_worker() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="argus-golden-config-") as tmp:
        tmp_root = Path(tmp).resolve()
        env = {name: os.environ[name] for name in PASSTHROUGH_ENV if name in os.environ}
        env.update({
            "HOME": str(tmp_root / "home"),
            "XDG_CACHE_HOME": str(tmp_root / "xdg" / "cache"),
            "XDG_CONFIG_HOME": str(tmp_root / "xdg" / "config"),
            "XDG_DATA_HOME": str(tmp_root / "xdg" / "data"),
            "XDG_STATE_HOME": str(tmp_root / "xdg" / "state"),
            "TMPDIR": str(tmp_root / "tmp"),
            "LC_ALL": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": str(REPO_ROOT),
            "TZ": "UTC",
        })
        for key in ("home", "tmp", "xdg/cache", "xdg/config", "xdg/data", "xdg/state"):
            (tmp_root / key).mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [sys.executable, "-B", str(Path(__file__).resolve()), "--_worker", str(tmp_root)],
            cwd=tmp_root, env=env, capture_output=True, text=True, encoding="utf-8",
            timeout=600, check=False,
        )
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            raise SystemExit(f"golden export worker failed with exit code {proc.returncode}")
        if proc.stderr.strip():
            sys.stderr.write(proc.stderr)
        return json.loads(proc.stdout)


def _manifest(results: dict[str, Any]) -> dict[str, Any]:
    import yaml

    hashes = _source_hashes()
    cases: dict[str, list[str]] = {category: [] for category in CATEGORIES}
    outcomes: dict[str, dict[str, int]] = {}
    for case_id in sorted(results):
        category = results[case_id]["category"]
        cases[category].append(case_id)
        counts = outcomes.setdefault(category, {"error": 0, "loaded": 0})
        counts[results[case_id]["outcome"]] += 1
    try:
        from importlib.metadata import version

        package_version = version("argus-app-testing")
    except Exception:  # noqa: BLE001 - informational only
        package_version = "unknown"
    return {
        "cases": cases,
        "counts": outcomes,
        "generated_with": {
            "_note": "informational; ignored by --check",
            "argus_package_version": package_version,
            "git_commit": _git("rev-parse", "HEAD"),
            "loader_sources_last_commit": _git("log", "-1", "--format=%H", "--",
                                               *LOADER_SOURCES),
            "python": ".".join(str(part) for part in sys.version_info[:3]),
            "pyyaml": yaml.__version__,
        },
        "generator": "scripts/golden/export_config.py",
        "implementation": {
            "loader_sources_sha256": _combined_source_hash(hashes),
            "name": "argus (Python)",
            "sources": hashes,
        },
        "schema": SCHEMA,
    }


def _comparable_manifest(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if isinstance(data, dict):
        data.pop("generated_with", None)
    return render(data)


def build_outputs() -> dict[Path, str]:
    results = _run_worker()
    outputs = {}
    for case in discover_cases():
        outputs[case.expected_path] = render(results[case.case_id])
    outputs[EXPECTED_DIR / MANIFEST_NAME] = render(_manifest(results))
    return outputs


def _existing_expected() -> list[Path]:
    if not EXPECTED_DIR.is_dir():
        return []
    return sorted(path for path in EXPECTED_DIR.rglob("*.json") if path.is_file())


def write(outputs: dict[Path, str]) -> int:
    for stale in _existing_expected():
        if stale not in outputs:
            stale.unlink()
            print(f"removed stale {stale.relative_to(GOLDEN_DIR).as_posix()}")
    changed = 0
    for path, text in sorted(outputs.items()):
        path.parent.mkdir(parents=True, exist_ok=True)
        data = text.encode("utf-8")
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)
            changed += 1
    print(f"wrote {len(outputs)} expected files ({changed} changed)")
    return 0


def check(outputs: dict[Path, str]) -> int:
    problems = []
    for path, text in sorted(outputs.items()):
        rel = path.relative_to(EXPECTED_DIR).as_posix()
        if not path.exists():
            problems.append((rel, "missing expected file", []))
            continue
        current = path.read_bytes().decode("utf-8", errors="replace")
        want, have = text, current
        if path.name == MANIFEST_NAME:
            want, have = _comparable_manifest(text), _comparable_manifest(current)
        if want != have:
            diff = list(difflib.unified_diff(
                have.splitlines(), want.splitlines(), f"committed/{rel}", f"generated/{rel}",
                lineterm="", n=2))
            problems.append((rel, "differs", diff))
    for path in _existing_expected():
        if path not in outputs:
            problems.append((path.relative_to(EXPECTED_DIR).as_posix(),
                             "stale expected file (no matching case)", []))
    if not problems:
        print(f"OK: {len(outputs) - 1} cases match expected/ (no drift)")
        return 0
    for rel, reason, diff in problems:
        print(f"DRIFT {rel}: {reason}")
        for line in diff[:40]:
            print(f"    {line}")
        if len(diff) > 40:
            print(f"    ... {len(diff) - 40} more diff lines")
    print(f"FAILED: {len(problems)} expected file(s) drifted; "
          "run scripts/golden/export_config.py --write and review the diff")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help="regenerate expected/")
    mode.add_argument("--check", action="store_true", help="fail when expected/ drifts")
    mode.add_argument("--_worker", metavar="TMP_ROOT", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker:
        return worker(Path(args._worker))
    if not sys.platform.startswith("linux"):
        print("expected/ is generated on Linux (see tests/golden/config/README.md); "
              f"this host is {sys.platform}", file=sys.stderr)
        return 2
    outputs = build_outputs()
    return write(outputs) if args.write else check(outputs)


if __name__ == "__main__":
    sys.exit(main())
