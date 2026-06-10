"""Run results — structured outcomes persisted to ``.argus/runs/``."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

STATUSES = ("pass", "fail", "error", "running", "skipped")


@dataclass
class StepResult:
    index: int
    kind: str          # setup | step | assert | teardown
    text: str
    status: str = "pending"
    duration_s: float = 0.0
    actions: List[str] = field(default_factory=list)   # what the agent actually did
    expected: Optional[str] = None
    actual: Optional[str] = None
    note: Optional[str] = None


@dataclass
class RunResult:
    test_name: str
    test_file: str
    adapter: str
    provider: str
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    duration_s: float = 0.0
    steps: List[StepResult] = field(default_factory=list)
    tokens: dict = field(default_factory=dict)
    error: Optional[str] = None

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
        """0 = all pass, 1 = assertion/step failure, 2 = error/crash."""
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

    def save(self, project_dir: Path) -> Path:
        runs_dir = project_dir / ".argus" / "runs"
        runs_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started_at))
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in self.test_file)
        path = runs_dir / f"{stamp}-{safe}.json"
        path.write_text(json.dumps(self.to_dict(), indent=2))
        return path


def load_runs(project_dir: Path, limit: int = 50) -> List[dict]:
    runs_dir = project_dir / ".argus" / "runs"
    if not runs_dir.is_dir():
        return []
    out = []
    for path in sorted(runs_dir.glob("*.json"), reverse=True)[:limit]:
        try:
            out.append(json.loads(path.read_text()))
        except (json.JSONDecodeError, OSError):
            continue
    return out
