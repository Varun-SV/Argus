"""Run results — structured outcomes persisted to ``.argus/runs/``."""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

STATUSES = ("pass", "fail", "error", "running", "skipped")
_storage_order_lock = threading.Lock()
_last_storage_order = 0


def _next_storage_order() -> int:
    """Keep local allocation order increasing across clock ties or rollback."""
    global _last_storage_order
    with _storage_order_lock:
        _last_storage_order = max(time.time_ns(), _last_storage_order + 1)
        return _last_storage_order


_HISTORY_NAME = re.compile(r"(\d{8}-\d{6})-(\d{20})-[0-9a-f]{32}-(.*)\.json")


_ORDER_FILE = ".storage-order"
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


_LOCK_WAIT_S = 30.0


@contextmanager
def _file_locked(stream, timeout: float = _LOCK_WAIT_S):
    """Hold an exclusive OS lock on ``stream`` (shared by every process on this project).

    Only contention is retried, for at most ``timeout`` seconds; any other locking
    error (unsupported filesystem, bad handle) raises at once so callers fail closed.
    """
    deadline = time.monotonic() + timeout
    if os.name == "nt":
        import msvcrt
        busy = {errno.EACCES, errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK)}

        def acquire():
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

        def release():
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        busy = {errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES}

        def acquire():
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        def release():
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    while True:
        try:
            acquire()
            break
        except OSError as exc:
            if exc.errno not in busy:
                raise
            if time.monotonic() >= deadline:
                raise OSError(errno.ETIMEDOUT, "Timed out waiting for the run history lock") from exc
            time.sleep(0.05)
    try:
        yield
    finally:
        release()


def reserve_storage_order(project_dir: Path) -> int:
    """Atomically reserve the project's next run order, across processes.

    The in-process counter can't see other ``argus run`` processes, and after a clock
    rollback two of them would otherwise pick the same next order. A project-scoped
    counter, read and advanced under an OS file lock, makes every reservation unique
    and later than all persisted history.
    """
    runs_root = _runs_root(project_dir)
    path = runs_root / _ORDER_FILE
    if path.is_symlink():
        raise OSError(f"run order counter cannot be a symlink: {path}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with _storage_order_lock, open(os.open(path, flags, 0o600), "r+b") as stream:
        _require_private_counter(stream, path)
        with _file_locked(stream):
            return _advance_counter(stream, runs_root)


def _require_private_counter(stream, path: Path) -> None:
    """Refuse a counter that is (or leads to) anything but this project's own file.

    It is rewritten on every run, so a hard link, reparse point or symlink planted at
    the path would let a run overwrite an unrelated file. The checks bind to the opened
    handle: the path must still name exactly that singly linked regular file.
    """
    opened = os.fstat(stream.fileno())
    named = os.lstat(path)
    if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
            or stat.S_ISLNK(named.st_mode)
            or getattr(named, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
            or getattr(opened, "st_file_attributes", 0) & _FILE_ATTRIBUTE_REPARSE_POINT
            or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)):
        raise OSError(f"run order counter must be a singly linked regular file in the project: {path}")


def _advance_counter(stream, runs_root: Path) -> int:
    """Read, advance and durably write the counter; the caller holds the file lock."""
    global _last_storage_order
    stream.seek(0)
    try:
        counter = int(stream.read(32).decode("ascii").strip() or 0)
    except (UnicodeError, ValueError):
        counter = 0
    # History written before the counter existed (or with it deleted) still counts.
    names = [entry.name for entry in runs_root.glob("*.json")]
    persisted = max((int(m[2]) for m in map(_HISTORY_NAME.fullmatch, names) if m), default=0)
    order = max(time.time_ns(), counter + 1, persisted + 1, _last_storage_order + 1)
    stream.seek(0)
    stream.truncate()
    stream.write(str(order).encode("ascii"))
    stream.flush()
    os.fsync(stream.fileno())
    _last_storage_order = order
    return order


def _runs_root(project_dir: Path) -> Path:
    """Return the canonical runs root and reject project-boundary escapes."""
    project_root = Path(project_dir).resolve(strict=True)

    # Attest .argus before creating anything beneath it. If a pre-existing
    # symlink/junction redirects .argus outside the project, fail without first
    # creating a runs directory through that redirect.
    argus_dir = project_root / ".argus"
    if argus_dir.is_symlink():
        raise OSError(f".argus cannot be a symlink: {argus_dir}")
    argus_dir.mkdir(exist_ok=True)
    resolved_argus = argus_dir.resolve(strict=True)
    try:
        resolved_argus.relative_to(project_root)
    except ValueError as exc:
        raise OSError(f".argus escapes the project root: {argus_dir}") from exc

    runs_dir = resolved_argus / "runs"
    if runs_dir.is_symlink():
        raise OSError(f".argus/runs cannot be a symlink: {runs_dir}")
    runs_dir.mkdir(exist_ok=True)
    resolved = runs_dir.resolve(strict=True)
    try:
        resolved.relative_to(resolved_argus)
    except ValueError as exc:
        raise OSError(f".argus/runs escapes .argus: {runs_dir}") from exc
    return resolved


class UnreservedRunError(OSError):
    """A result whose cross-process history order was never reserved can't be saved."""


@dataclass
class StepResult:
    index: int
    kind: str
    text: str
    status: str = "pending"
    duration_s: float = 0.0
    actions: List[str] = field(default_factory=list)
    expected: Optional[str] = None
    actual: Optional[str] = None
    note: Optional[str] = None
    flaky: bool = False
    screenshot_path: Optional[str] = None


@dataclass
class RunResult:
    test_name: str
    test_file: str
    adapter: str
    provider: str
    environment_type: str = "direct"
    isolated: bool = False
    location: str = "unknown"
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    duration_s: float = 0.0
    steps: List[StepResult] = field(default_factory=list)
    tokens: dict = field(default_factory=dict)
    error: Optional[str] = None
    staged_files: List[dict] = field(default_factory=list)
    artifacts: List[dict] = field(default_factory=list)
    transfer_error: Optional[str] = None
    failure_capsule: Optional[dict] = None
    failure_capsule_error: Optional[dict] = None

    def __post_init__(self):
        # Storage identity is private: it is not an ATES/Capsule identifier or
        # part of the public result document. Allocation order keeps same-second
        # history chronological, even when the test filename repeats.
        self._storage_order = _next_storage_order()
        self._storage_id = uuid.uuid4().hex
        self._storage_lock = threading.RLock()
        self._owned_run_dirs = {}
        self._owned_history_files = {}
        self._unreserved: Optional[str] = None

    def claim_project_order(self, project_dir: Path) -> None:
        """Take a cross-process order from the project counter before the first save.

        If that fails the result keeps no trustworthy place in history, so ``save``
        refuses it rather than publish it under this process's unverified order.
        """
        with self._storage_lock:
            if not self._owned_run_dirs:
                try:
                    self._storage_order = reserve_storage_order(project_dir)
                except OSError as exc:
                    self._unreserved = str(exc)
                    raise
                self._unreserved = None

    @property
    def passed(self) -> int:
        return sum(1 for s in self.steps if s.status == "pass")

    @property
    def failed(self) -> int:
        return sum(1 for s in self.steps if s.status in ("fail", "error"))

    @property
    def skipped(self) -> int:
        return sum(1 for s in self.steps if s.status == "skipped")

    @property
    def exit_code(self) -> int:
        if self.status == "pass":
            return 0
        if self.status == "error":
            return 2
        return 1

    def summary_line(self) -> str:
        return (
            f"{self.passed} passed · {self.failed} failed · {self.skipped} skipped "
            f"· {self.duration_s:.1f}s · exit {self.exit_code}"
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def run_dir(self, project_dir: Path) -> Path:
        with self._storage_lock:
            runs_root = _runs_root(project_dir)
            owned = self._owned_run_dirs.get(runs_root)
            if owned is not None:
                candidate, identity = owned
                if candidate.is_symlink():
                    raise OSError(f"run directory cannot be a symlink: {candidate}")
                st = candidate.stat()
                if (st.st_dev, st.st_ino) != identity or not candidate.is_dir():
                    raise OSError("run directory ownership changed")
                return candidate
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started_at))
            safe = _bounded_name(self.test_file)
            for _ in range(10):
                candidate = runs_root / f"{stamp}-{self._storage_order:020d}-{self._storage_id}-{safe}"
                flat = runs_root / f"{candidate.name}.json"
                if flat.exists() or flat.is_symlink():
                    self._storage_id = uuid.uuid4().hex
                    continue
                try:
                    candidate.mkdir(exist_ok=False)
                except FileExistsError:
                    self._storage_id = uuid.uuid4().hex
                    continue
                if candidate.is_symlink():
                    raise OSError(f"run directory cannot be a symlink: {candidate}")
                resolved = candidate.resolve(strict=True)
                try:
                    resolved.relative_to(runs_root)
                except ValueError as exc:
                    raise OSError(f"run directory escapes .argus/runs: {candidate}") from exc
                st = resolved.stat()
                self._owned_run_dirs[runs_root] = (resolved, (st.st_dev, st.st_ino))
                return resolved
            raise OSError("Could not reserve a unique run directory")

    def save(self, project_dir: Path) -> Path:
        with self._storage_lock:
            if self._unreserved is not None:
                raise UnreservedRunError(
                    f"Not added to run history: its place could not be reserved ({self._unreserved}).")
            run_dir = self.run_dir(project_dir)
            data = json.dumps(self.to_dict(), indent=2)
            (run_dir / "result.json").write_text(data, encoding="utf-8")
            write_report(self, run_dir)
            flat = run_dir.parent / f"{run_dir.name}.json"
            identity = self._owned_history_files.get(flat)
            # Reserve history exclusively on the first save. Re-saving may
            # update only the same file this result instance originally created.
            with flat.open("x" if identity is None else "r+", encoding="utf-8") as stream:
                st = os.fstat(stream.fileno())
                current = (st.st_dev, st.st_ino)
                if identity is not None and current != identity:
                    raise OSError("run history ownership changed")
                self._owned_history_files[flat] = current
                stream.seek(0)
                stream.truncate()
                stream.write(data)
            return run_dir / "result.json"


def write_report(result: RunResult, run_dir: Path) -> Path:
    """Write a markdown report with per-step screenshots and transfer provenance."""
    lines = [
        "# Argus Test Report",
        "",
        f"- **Test:** `{result.test_name}`",
        f"- **Adapter:** `{result.adapter}`",
        f"- **Environment:** `{result.environment_type}`",
        f"- **Isolated:** {'yes' if result.isolated else 'no'}",
        f"- **Location:** `{result.location}`",
        f"- **Provider:** `{result.provider}`",
        f"- **Status:** {result.status.upper()}",
        f"- **Duration:** {result.duration_s:.1f}s",
        f"- **Tokens:** {result.tokens.get('total_tokens', 0)} "
        f"({result.tokens.get('calls', 0)} LLM calls)",
        f"- **Staged files:** {len(result.staged_files)}",
        f"- **Collected artifacts:** {len(result.artifacts)}",
    ]
    if result.failure_capsule:
        lines += [
            f"- **Failure Capsule:** `{result.failure_capsule.get('failure_id', 'retained')}`",
            f"- **Failure VM:** `{result.failure_capsule.get('vm_name', 'unknown')}`",
            f"- **Failure state:** `{result.failure_capsule.get('vm_state', 'unknown')}`",
            f"- **Failure storage:** `{result.failure_capsule.get('root_dir', 'unknown')}`",
        ]
    if result.failure_capsule_error:
        lines += [
            "- **Failure Capsule retention:** ⚠️ retention failed; Capsule preserved for recovery",
            f"- **Recovery VM:** `{result.failure_capsule_error.get('vm_name', 'unknown')}`",
            f"- **Recovery storage:** `{result.failure_capsule_error.get('root_dir', 'unknown')}`",
        ]
    lines += [
        "",
        "| # | Step | Kind | Status | Duration |",
        "|---|------|------|--------|----------|",
    ]
    for sr in result.steps:
        status_icon = {"pass": "✅", "fail": "❌", "error": "💥", "skipped": "⏭"}.get(sr.status, sr.status)
        lines.append(
            f"| {sr.index + 1} | {sr.text[:60]} | {sr.kind} "
            f"| {status_icon} {sr.status} | {sr.duration_s:.1f}s |"
        )
    lines.append("")

    if result.error:
        lines += [f"**Error:** {result.error}", ""]
    if result.transfer_error:
        lines += [f"**Transfer error:** {result.transfer_error}", ""]

    if result.staged_files:
        lines += ["## Staged files", "", "| Source | Guest destination | Size | SHA-256 |", "|---|---|---:|---|"]
        for item in result.staged_files:
            lines.append(
                f"| `{item.get('source', '')}` | `{item.get('destination', '')}` | "
                f"{item.get('size', 0)} | `{item.get('sha256', '')}` |"
            )
        lines.append("")

    if result.artifacts:
        lines += ["## Collected artifacts", "", "| Guest path | Host artifact | Size | SHA-256 |", "|---|---|---:|---|"]
        for item in result.artifacts:
            lines.append(
                f"| `{item.get('path', '')}` | `{item.get('host_path', '')}` | "
                f"{item.get('size', 0)} | `{item.get('sha256', '')}` |"
            )
        lines.append("")

    if result.failure_capsule:
        lines += [
            "## Failure Capsule",
            "",
            "Argus retained the Capsule before teardown so the failed VM disk/configuration can be inspected or reproduced.",
            "",
            f"- **Reason:** {result.failure_capsule.get('reason', 'test failure')}",
            f"- **VM:** `{result.failure_capsule.get('vm_name', 'unknown')}`",
            f"- **State:** `{result.failure_capsule.get('vm_state', 'unknown')}`",
            f"- **Storage:** `{result.failure_capsule.get('root_dir', 'unknown')}`",
            "",
        ]

    if result.failure_capsule_error:
        lines += [
            "## Failure Capsule retention error",
            "",
            "Argus could not complete the requested retention operation. To avoid destroying evidence, the Capsule was left registered and its session storage was preserved.",
            "",
            f"- **Error:** {result.failure_capsule_error.get('error', 'unknown retention error')}",
            f"- **VM:** `{result.failure_capsule_error.get('vm_name', 'unknown')}`",
            f"- **Storage:** `{result.failure_capsule_error.get('root_dir', 'unknown')}`",
            f"- **Recovery:** {result.failure_capsule_error.get('recovery', 'inspect the preserved Capsule manually')}",
            "",
        ]

    lines.append("## Steps")
    lines.append("")
    for sr in result.steps:
        status_icon = {"pass": "✅", "fail": "❌", "error": "💥", "skipped": "⏭"}.get(sr.status, sr.status)
        lines += [f"### Step {sr.index + 1}: {sr.text}", ""]
        lines.append(f"**Status:** {status_icon} `{sr.status}`  ")
        lines.append(f"**Kind:** {sr.kind}  ")
        lines.append(f"**Duration:** {sr.duration_s:.1f}s")
        if sr.expected:
            lines.append(f"  \n**Expected:** {sr.expected}")
        if sr.actual:
            lines.append(f"  \n**Actual:** {sr.actual}")
        if sr.note:
            lines.append(f"  \n**Note:** {sr.note}")
        if sr.actions:
            lines.append("  \n**Actions taken:**")
            for a in sr.actions:
                lines.append(f"  - {a}")
        if sr.screenshot_path:
            lines += ["", f"![step {sr.index + 1} screenshot]({sr.screenshot_path})", ""]
        else:
            lines.append("")

    path = run_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def valid_run_history(data) -> bool:
    """Accept old result documents while rejecting shapes history readers cannot use."""
    if not isinstance(data, dict):
        return False
    for name in ("test_file", "test_name", "status", "provider", "adapter"):
        if name in data and not isinstance(data[name], str):
            return False
    steps = data.get("steps", [])
    tokens = data.get("tokens", {})
    if not isinstance(steps, list) or not isinstance(tokens, dict):
        return False
    if any(not isinstance(step, dict) or
           ("status" in step and not isinstance(step["status"], str)) for step in steps):
        return False
    for value in (data.get("duration_s", 0), tokens.get("total_tokens", 0)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            if not math.isfinite(value):
                return False
        except OverflowError:
            return False
    return True


# The prefix before the test name is "<stamp>-<order:20>-<uuid:32>-" (70 chars) and
# the flat history file adds ".json"; keep the whole component well under 255 bytes.
_MAX_NAME_PART = 120


def _bounded_name(test_file: str) -> str:
    """A filesystem-safe test-name component that can't overflow a path segment."""
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in test_file)
    if len(safe.encode("utf-8", "surrogatepass")) <= _MAX_NAME_PART:
        return safe
    digest = hashlib.sha256(test_file.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    head = safe
    while len(head.encode("utf-8", "surrogatepass")) > _MAX_NAME_PART - 13:
        head = head[:-1]
    return f"{head}-{digest}"


def run_history_paths(runs_dir: Path, limit: Optional[int] = None) -> List[Path]:
    """Order new and legacy filenames without reading every result document.

    Callers that validate documents should take every path and stop once they
    have ``limit`` valid rows, so malformed newest files can't hide older runs.
    """
    def order(path):
        match = _HISTORY_NAME.fullmatch(path.name)
        if match:
            # The monotonic allocation order is authoritative: it survives a wall clock
            # that moved backwards, which the formatted stamp does not.
            return 1, match[2], path.name
        # Legacy rows predate high-resolution allocation, so they sort before every new row.
        return 0, path.name[:15], path.name
    ordered = sorted(runs_dir.glob("*.json"), key=order, reverse=True)
    return ordered if limit is None else ordered[:limit]


def load_runs(project_dir: Path, limit: int = 50) -> List[dict]:
    runs_dir = project_dir / ".argus" / "runs"
    if not runs_dir.is_dir():
        return []
    out = []
    for path in run_history_paths(runs_dir):
        if len(out) >= limit:
            break
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if valid_run_history(data):
                out.append(data)
        except (json.JSONDecodeError, UnicodeError, OSError):
            continue
    return out
