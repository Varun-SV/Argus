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

import io
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.golden import _harness as harness  # noqa: E402 - path set up above
from scripts.golden.export_state import _block_network, render  # noqa: E402

SCHEMA = "argus-golden-ates-v1"
GOLDEN_DIR = REPO_ROOT / "tests" / "golden" / "ates"
PINNED_VERSION = "0.0.0+golden"

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


def _verify(run_dir: Path, project: Path) -> dict[str, Any]:
    from argus.ates import inspect_finalization_trust, verify_finalized_run, verify_report_bundle

    replace = {str(project): "<PROJECT>"}
    return {
        "inspect_finalization_trust": harness.attempt(
            lambda: inspect_finalization_trust(run_dir), replace),
        "verify_finalized_run": harness.attempt(lambda: verify_finalized_run(run_dir), replace),
        "verify_report_bundle": harness.attempt(lambda: verify_report_bundle(run_dir), replace),
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
    if not harness.worker_guard(tmp_root):
        return 2
    harness.Determinism("ates", PINNED_VERSION)
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


def build_outputs() -> dict[str, bytes]:
    return harness.build_outputs(
        Path(__file__).resolve(), "argus-golden-ates-",
        lambda files: harness.manifest(
            files, schema=SCHEMA, generator="scripts/golden/export_ates.py", sources=SOURCES,
            cases=list(CASES), pinned_version=PINNED_VERSION))


def main(argv: list[str] | None = None) -> int:
    return harness.main(argv, doc=__doc__ or "", golden_dir=GOLDEN_DIR,
                        script=Path(__file__).resolve(), worker=worker, build=build_outputs)


if __name__ == "__main__":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.exit(main())
