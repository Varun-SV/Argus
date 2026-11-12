"""A workspace: a folder holding tests (``.argus/tests``) and results (``.argus/results``)."""

from __future__ import annotations

import json
import re
import secrets
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from argus2.spec import SpecError, parse_spec

_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._ -]{0,80}\.yaml$")
_ID = re.compile(r"^[0-9]{8}-[0-9]{6}-[0-9a-f]{6}$")

SAMPLES = {
    "sample-command-line.yaml": """\
name: The echo command prints what it is given
target:
  type: cli
steps:
  - run: echo hello from argus
  - check:
      output_contains: hello from argus
      exit_code: 0
""",
    "sample-website.yaml": """\
name: The example.com page explains what it is for
target:
  type: web
  url: https://example.com
steps:
  - check:
      text_visible: Example Domain
  - verify: The page explains that this domain is for use in examples
""",
}


class StoreError(ValueError):
    pass


def slug(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:50]
    return text or "test"


class Workspace:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise StoreError(f"This folder does not exist: {self.root}")
        self.tests_dir = self.root / ".argus" / "tests"
        self.results_dir = self.root / ".argus" / "results"

    # -- tests -----------------------------------------------------------
    def _test_path(self, file: str) -> Path:
        if not isinstance(file, str) or not _FILE.match(file):
            raise StoreError("A test file name may use letters, numbers, spaces, dots, dashes and "
                             "underscores, and must end in .yaml")
        return self.tests_dir / file

    def list_tests(self) -> List[Dict[str, Any]]:
        last: Dict[str, Dict[str, Any]] = {}
        for result in self.list_results(400):
            if result.get("file") and result["file"] not in last:
                last[result["file"]] = {"status": result["status"], "id": result["id"], "started": result["started"]}
        tests = []
        if self.tests_dir.is_dir():
            for path in sorted(self.tests_dir.glob("*.yaml"), key=lambda p: p.name.lower()):
                entry: Dict[str, Any] = {"file": path.name, "last": last.get(path.name)}
                try:
                    spec = parse_spec(path.read_text(encoding="utf-8"))
                    entry.update(name=spec.name, target=spec.target, steps=len(spec.steps),
                                 needs_model=spec.needs_model, error="")
                except (SpecError, OSError, UnicodeDecodeError) as exc:
                    entry.update(name=path.stem, target=None, steps=0, needs_model=False, error=str(exc))
                tests.append(entry)
        return tests

    def read_test(self, file: str) -> str:
        path = self._test_path(file)
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise StoreError(f"There is no test called {file}.") from None

    def write_test(self, file: str, content: str, overwrite: bool = True) -> None:
        path = self._test_path(file)
        parse_spec(content)  # never save something that cannot run
        if not overwrite and path.exists():
            raise StoreError(f"A test called {file} already exists.")
        self.tests_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)

    def free_name(self, name: str) -> str:
        base = slug(name)
        candidate, number = f"{base}.yaml", 2
        while (self.tests_dir / candidate).exists():
            candidate, number = f"{base}-{number}.yaml", number + 1
        return candidate

    def delete_test(self, file: str) -> None:
        path = self._test_path(file)
        try:
            path.unlink()
        except FileNotFoundError:
            raise StoreError(f"There is no test called {file}.") from None

    def add_samples(self) -> List[str]:
        added = []
        self.tests_dir.mkdir(parents=True, exist_ok=True)
        for name, content in SAMPLES.items():
            if not (self.tests_dir / name).exists():
                (self.tests_dir / name).write_text(content, encoding="utf-8")
                added.append(name)
        return added

    # -- results ---------------------------------------------------------
    def new_result_dir(self) -> Path:
        self.results_dir.mkdir(parents=True, exist_ok=True)
        while True:
            path = self.results_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"
            try:
                path.mkdir()
                return path
            except FileExistsError:
                continue

    def save_result(self, directory: Path, result: Dict[str, Any]) -> None:
        tmp = directory / "result.json.tmp"
        tmp.write_text(json.dumps(result, indent=2), encoding="utf-8")
        tmp.replace(directory / "result.json")

    def _result_dir(self, result_id: str) -> Path:
        if not isinstance(result_id, str) or not _ID.match(result_id):
            raise StoreError("Unknown result.")
        return self.results_dir / result_id

    def get_result(self, result_id: str) -> Dict[str, Any]:
        try:
            data = json.loads((self._result_dir(result_id) / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise StoreError("This result could not be read.") from None
        if not isinstance(data, dict):
            raise StoreError("This result could not be read.")
        return data

    def list_results(self, limit: int = 100) -> List[Dict[str, Any]]:
        if not self.results_dir.is_dir():
            return []
        out = []
        names = sorted((p.name for p in self.results_dir.iterdir() if _ID.match(p.name)), reverse=True)
        for name in names[:limit]:
            try:
                data = self.get_result(name)
            except StoreError:
                continue
            steps = data.get("steps") or []
            out.append({
                "id": name, "kind": data.get("kind"), "name": data.get("name"), "file": data.get("file"),
                "status": data.get("status"), "started": data.get("started"),
                "duration": data.get("duration"), "target": data.get("target"),
                "summary": data.get("summary", ""), "model": data.get("model", ""),
                "tokens": data.get("tokens"),
                "passed_steps": sum(1 for s in steps if s.get("status") == "passed"),
                "total_steps": len(steps), "findings": len(data.get("findings") or []),
            })
        return out

    def artifact(self, result_id: str, name: str) -> Path:
        if not re.match(r"^[A-Za-z0-9._-]{1,80}$", name or "") or name.startswith("."):
            raise StoreError("Unknown file.")
        path = self._result_dir(result_id) / name
        if not path.is_file():
            raise StoreError("Unknown file.")
        return path

    def delete_result(self, result_id: str) -> None:
        shutil.rmtree(self._result_dir(result_id), ignore_errors=True)


def default_workspace() -> Path:
    path = Path.home() / "Argus"
    path.mkdir(parents=True, exist_ok=True)
    return path


def browse(path: Optional[str]) -> Dict[str, Any]:
    """Folders inside ``path`` (for the workspace picker)."""
    base = Path(path).expanduser() if path else Path.home()
    try:
        base = base.resolve()
        folders = sorted((p.name for p in base.iterdir() if p.is_dir() and not p.name.startswith(".")),
                         key=str.lower)
    except OSError as exc:
        raise StoreError(f"Cannot open {base}: {exc.strerror or exc}") from None
    parent = str(base.parent) if base.parent != base else ""
    return {"path": str(base), "parent": parent, "folders": folders[:500]}
