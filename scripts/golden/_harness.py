"""Shared parent-side machinery for the golden exporters added in P0.

``export_ates.py``, ``export_fleet.py`` and ``export_capsule.py`` all run their writer in a
child process with a scrubbed environment (the same environment ``export_state.py`` uses), then
write or check the generated files against ``tests/golden/<name>/``. This module holds that
shared part, plus the deterministic patches and the JSON view of Python values that their
workers use.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.golden.export_state import (  # noqa: E402 - path set up above
    CONSOLE_WIDTH,
    _sorted_directory_listing,
    PASSTHROUGH_ENV,
    _comparable_manifest,
    _git,
    _sha256,
    _text_diff,
    render,
)

__all__ = [
    "CLOCK_START",
    "Determinism",
    "build_outputs",
    "check",
    "manifest",
    "plain",
    "render",
    "write",
]

CLOCK_START = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)
MANIFEST_NAME = "_manifest.json"
# Hand-written files that live next to the generated ones.
UNGENERATED = ("README.md", ".gitattributes")


# --------------------------------------------------------------------------- worker side


class Determinism:
    """Documented monkeypatches that make the Python writers reproducible.

    Every ``argus`` module is imported first, so modules a writer would import lazily are
    patched too. Then: ``datetime.datetime`` in every ``argus.*`` module becomes a subclass
    whose ``now()`` starts at ``CLOCK_START`` and advances 1 ms per call; ``time.time``,
    ``time.time_ns``, ``time.monotonic`` and ``time.perf_counter`` advance in the same steps;
    ``uuid.uuid4``, ``secrets.token_bytes``/``token_hex``/``token_urlsafe`` and ``os.urandom``
    return SHA-256 counters over ``label``; ``argus.__version__`` is pinned; and
    ``os.listdir`` and ``Path.glob``/``rglob``/``iterdir`` return sorted results.
    """

    def __init__(self, label: str, pinned_version: str) -> None:
        import base64
        import pkgutil
        import secrets as _secrets
        import time as _time
        import uuid as _uuid

        import argus

        for info in pkgutil.walk_packages(argus.__path__, "argus."):
            if info.name.startswith(("argus.gui", "argus.serve")):
                continue
            try:
                __import__(info.name)
            except Exception:  # noqa: BLE001 - optional dependencies
                pass

        self._label = label
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

        # Read the real version once: the loop below pins argus.__version__ itself, so it
        # cannot be the comparison value.
        real_version = argus.__version__
        for name, module in list(sys.modules.items()):
            if not name.startswith("argus") or module is None:
                continue
            if getattr(module, "datetime", None) is _dt.datetime:
                module.datetime = _FrozenDatetime  # type: ignore[attr-defined]
            if getattr(module, "__version__", None) == real_version:
                module.__version__ = pinned_version  # type: ignore[attr-defined]
        _time.time = self._seconds  # type: ignore[assignment]
        _time.time_ns = self._nanoseconds  # type: ignore[assignment]
        _time.monotonic = self._seconds  # type: ignore[assignment]
        _time.perf_counter = self._seconds  # type: ignore[assignment]
        _uuid.uuid4 = self._uuid4  # type: ignore[assignment]
        _secrets.token_bytes = self.bytes  # type: ignore[assignment]
        _secrets.token_hex = lambda n=32: self.bytes(n).hex()  # type: ignore[assignment]
        _secrets.token_urlsafe = (  # type: ignore[assignment]
            lambda n=32: base64.urlsafe_b64encode(self.bytes(n)).rstrip(b"=").decode("ascii"))
        os.urandom = self.bytes  # type: ignore[assignment]
        # Directory enumeration order is up to the filesystem; see export_state.py.
        _sorted_directory_listing()

    def snapshot(self) -> tuple[int, dict[str, int]]:
        """The clock and counter state, for code whose own consumption must not count."""
        return self._ticks, dict(self._counters)

    def restore(self, state: tuple[int, dict[str, int]]) -> None:
        self._ticks, self._counters = state[0], dict(state[1])

    def _tick(self) -> int:
        self._ticks += 1
        return self._ticks

    def _next_datetime(self) -> _dt.datetime:
        return CLOCK_START + _dt.timedelta(milliseconds=self._tick())

    def _seconds(self) -> float:
        return CLOCK_START.timestamp() + self._tick() / 1000

    def _nanoseconds(self) -> int:
        return int(CLOCK_START.timestamp()) * 1_000_000_000 + self._tick() * 1_000_000

    def derive(self, kind: str, n: int) -> bytes:
        """``n`` reproducible bytes: SHA-256 over the label, ``kind`` and a counter."""
        index = self._counters.get(kind, 0) + 1
        self._counters[kind] = index
        out, block = b"", 0
        while len(out) < n:
            seed = f"argus-golden-{self._label}-{kind}-{index}-{block}".encode("ascii")
            out += hashlib.sha256(seed).digest()
            block += 1
        return out[:n]

    def _uuid4(self):
        import uuid as _uuid

        return _uuid.UUID(bytes=self.derive("uuid", 16), version=4)

    def bytes(self, n: int | None = None) -> bytes:
        return self.derive("bytes", 32 if n is None else n)


def plain(value: Any) -> Any:
    """JSON-compatible view of Python values (dataclasses, enums, paths, datetimes)."""
    from dataclasses import fields, is_dataclass
    from enum import Enum

    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: plain(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, _dt.datetime):
        return value.isoformat()
    if isinstance(value, bytes):
        # Byte payloads (for example a manifest a verifier read back) are files that are
        # committed elsewhere; record their identity rather than their content.
        return {"sha256": "sha256:" + hashlib.sha256(value).hexdigest(), "size": len(value)}
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, frozenset, set)):
        items = [plain(v) for v in value]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return repr(value)


def attempt(fn: Callable[[], Any], replace: Mapping[str, str] | None = None) -> dict[str, Any]:
    """``{"value": ...}`` or ``{"error": {"type", "message"}}``; rejections are data."""
    try:
        text = json.dumps(plain(fn()), sort_keys=True)
    except Exception as exc:  # noqa: BLE001 - rejections are part of the contract
        message = str(exc)
        for needle, token in (replace or {}).items():
            message = message.replace(needle, token)
        return {"error": {"message": message, "type": type(exc).__name__}}
    for needle, token in (replace or {}).items():
        text = text.replace(json.dumps(needle)[1:-1], json.dumps(token)[1:-1])
    return {"value": json.loads(text)}


# --------------------------------------------------------------------------- parent side


def source_hashes(sources: tuple[str, ...]) -> dict[str, str]:
    return {rel: _sha256((REPO_ROOT / rel).read_bytes().replace(b"\r\n", b"\n"))
            for rel in sources}


def manifest(files: Mapping[str, bytes], *, schema: str, generator: str,
             sources: tuple[str, ...], **extra: Any) -> dict[str, Any]:
    hashes = source_hashes(sources)
    combined = _sha256("".join(f"{rel}\0{hashes[rel]}\n" for rel in sorted(hashes)).encode())
    try:
        from importlib.metadata import version

        package_version = version("argus-app-testing")
    except Exception:  # noqa: BLE001 - informational only
        package_version = "unknown"
    payload = {
        "files": {rel: {"bytes": len(data), "sha256": _sha256(data)}
                  for rel, data in sorted(files.items())},
        "generated_with": {
            "_note": "informational; ignored by --check",
            "argus_package_version": package_version,
            "git_commit": _git("rev-parse", "HEAD"),
            "python": ".".join(str(part) for part in sys.version_info[:3]),
            "sources_last_commit": _git("log", "-1", "--format=%H", "--", *sources),
        },
        "generator": generator,
        "implementation": {
            "name": "argus (Python)",
            "sources": hashes,
            "sources_sha256": combined,
        },
        "schema": schema,
    }
    payload.update(extra)
    return payload


def build_outputs(script: Path, prefix: str,
                  make_manifest: Callable[[dict[str, bytes]], dict[str, Any]]) -> dict[str, bytes]:
    """Run ``script --_worker TMP`` in a scrubbed environment and collect ``TMP/out``."""
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
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
            [sys.executable, "-B", str(script), "--_worker", str(tmp_root)],
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
    files[MANIFEST_NAME] = render(make_manifest(files)).encode("utf-8")
    return files


def worker_guard(tmp_root: Path) -> bool:
    """True when the worker really runs inside the scrubbed export directory."""
    home = Path(os.environ.get("HOME", "")).resolve()
    if tmp_root.resolve() not in home.parents:
        print("refusing to run: HOME is not redirected into the export directory",
              file=sys.stderr)
        return False
    return True


def _existing(golden_dir: Path) -> list[str]:
    if not golden_dir.is_dir():
        return []
    return sorted(path.relative_to(golden_dir).as_posix() for path in golden_dir.rglob("*")
                  if path.is_file()
                  and path.relative_to(golden_dir).as_posix() not in UNGENERATED)


def write(golden_dir: Path, outputs: Mapping[str, bytes]) -> int:
    label = golden_dir.relative_to(REPO_ROOT).as_posix()
    for rel in _existing(golden_dir):
        if rel not in outputs:
            (golden_dir / rel).unlink()
            print(f"removed stale {rel}")
    changed = 0
    for rel, data in sorted(outputs.items()):
        path = golden_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_bytes() != data:
            path.write_bytes(data)
            changed += 1
    for directory in sorted(golden_dir.rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()
    print(f"wrote {len(outputs)} files under {label} ({changed} changed)")
    return 0


def check(golden_dir: Path, outputs: Mapping[str, bytes], script: str) -> int:
    label = golden_dir.relative_to(REPO_ROOT).as_posix()
    problems: list[tuple[str, str, list[str]]] = []
    for rel, data in sorted(outputs.items()):
        path = golden_dir / rel
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
    for rel in _existing(golden_dir):
        if rel not in outputs:
            problems.append((rel, "stale committed file (not generated)", []))
    if not problems:
        print(f"OK: {len(outputs)} files under {label} match (no drift)")
        return 0
    for rel, reason, diff in problems:
        print(f"DRIFT {rel}: {reason}")
        for line in diff[:40]:
            print(f"    {line}")
    print(f"FAILED: {len(problems)} file(s) drifted; run {script} --write and review the diff")
    return 1


def main(argv: list[str] | None, *, doc: str, golden_dir: Path, script: Path,
         worker: Callable[[Path], int], build: Callable[[], dict[str, bytes]]) -> int:
    import argparse

    label = golden_dir.relative_to(REPO_ROOT).as_posix()
    parser = argparse.ArgumentParser(description=(doc or "").split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true", help=f"regenerate {label}")
    mode.add_argument("--check", action="store_true", help=f"fail when {label} drifts")
    mode.add_argument("--_worker", metavar="TMP_ROOT", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args._worker:
        return worker(Path(args._worker))
    if not sys.platform.startswith("linux"):
        print(f"{label} is generated on Linux (see its README.md); this host is {sys.platform}",
              file=sys.stderr)
        return 2
    outputs = build()
    if args.write:
        return write(golden_dir, outputs)
    return check(golden_dir, outputs, script.relative_to(REPO_ROOT).as_posix())
