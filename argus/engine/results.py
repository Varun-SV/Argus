"""Run results — structured outcomes persisted to ``.argus/runs/``."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

STATUSES = ("pass", "fail", "error", "running", "skipped")


def _runs_root(project_dir: Path) -> Path:
    """Return the canonical runs root and reject project-boundary escapes."""
    project_root = Path(project_dir).resolve(strict=True)
    runs_dir = project_root / ".argus" / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    resolved = runs_dir.resolve(strict=True)
    try:
        resolved.relative_to(project_root)
    except ValueError as exc:
        raise OSError(f".argus/runs escapes the project root: {runs_dir}") from exc
    return resolved


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
        runs_root = _runs_root(project_dir)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started_at))
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in self.test_file)
        candidate = runs_root / f"{stamp}-{safe}"
        if candidate.is_symlink():
            raise OSError(f"run directory cannot be a symlink: {candidate}")
        candidate.mkdir(exist_ok=True)
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(runs_root)
        except ValueError as exc:
            raise OSError(f"run directory escapes .argus/runs: {candidate}") from exc
        return resolved

    def save(self, project_dir: Path) -> Path:
        runs_root = _runs_root(project_dir)
        run_dir = self.run_dir(project_dir)
        (run_dir / "result.json").write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
        write_report(self, run_dir)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started_at))
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in self.test_file)
        flat = runs_root / f"{stamp}-{safe}.json"
        flat.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
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


def load_runs(project_dir: Path, limit: int = 50) -> List[dict]:
    runs_dir = project_dir / ".argus" / "runs"
    if not runs_dir.is_dir():
        return []
    out = []
    for path in sorted(runs_dir.glob("*.json"), reverse=True)[:limit]:
        try:
            out.append(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            continue
    return out
