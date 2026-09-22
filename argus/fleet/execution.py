"""Node-side Fleet bridge into the existing Capsule ExecutionEnvironment.

This module owns the mutation boundary between a durable Fleet admission and one
Capsule launch. It deliberately has no local-execution fallback: a Fleet
placement either launches through an existing Capsule ExecutionEnvironment or
fails closed.
"""
from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from argus.execution.base import ExecutionEnvironment

from .placement import FleetPlacementError, NodeAdmissionStore, PlacementAuthorization, SessionRequest, StagedInputIdentity


@dataclass(frozen=True)
class FleetExecutionState:
    session_request_id: str
    placement_generation: int
    state: str


@dataclass(frozen=True)
class VerifiedLaunchInputs:
    """Private immutable-by-convention snapshots bound to one Fleet launch."""
    image_path: Path
    staged_paths: Mapping[str, Path]


class FleetNodeExecutor:
    """Idempotently translate one authorized Fleet placement into a Capsule."""

    def __init__(self, path: Path | str, *, admission_store: NodeAdmissionStore, environment_factory: Callable[[SessionRequest, VerifiedLaunchInputs, str], ExecutionEnvironment], image_path_resolver: Callable[[SessionRequest], Path | str], staged_path_resolver: Callable[[StagedInputIdentity], Path | str], execution_probe: Optional[Callable[[str], Optional[str]]] = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.admission_store = admission_store
        self.environment_factory = environment_factory
        self.image_path_resolver = image_path_resolver
        self.staged_path_resolver = staged_path_resolver
        self.execution_probe = execution_probe
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS fleet_node_executions (
                    session_request_id TEXT NOT NULL,
                    placement_generation INTEGER NOT NULL,
                    run_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    execution_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    PRIMARY KEY(session_request_id, placement_generation)
                );
                CREATE TABLE IF NOT EXISTS fleet_execution_reconciliation (
                    session_request_id TEXT NOT NULL,
                    placement_generation INTEGER NOT NULL,
                    observed_state TEXT NOT NULL,
                    PRIMARY KEY(session_request_id, placement_generation)
                );
            """)
        finally:
            conn.close()

    @staticmethod
    def _snapshot_file(source: Path, destination: Path) -> tuple[str, int]:
        digest = hashlib.sha256(); size = 0
        try:
            with source.open("rb") as src, destination.open("xb") as dst:
                while True:
                    block = src.read(1024 * 1024)
                    if not block: break
                    size += len(block); digest.update(block); dst.write(block)
                dst.flush()
        except OSError as exc:
            raise FleetPlacementError(f"cannot snapshot Fleet launch input: {source}") from exc
        return "sha256:" + digest.hexdigest(), size

    @staticmethod
    def _verify_file(path: Path, expected_digest: str, expected_size: Optional[int] = None) -> None:
        digest = hashlib.sha256(); size = 0
        try:
            with path.open("rb") as source:
                while True:
                    block = source.read(1024 * 1024)
                    if not block: break
                    size += len(block); digest.update(block)
        except OSError as exc:
            raise FleetPlacementError(f"cannot read retained Fleet launch input: {path}") from exc
        if "sha256:" + digest.hexdigest() != expected_digest or (expected_size is not None and size != expected_size):
            raise FleetPlacementError("retained Fleet launch input no longer matches authorized identity")

    def _retained_snapshot_root(self, execution_key: str) -> Path:
        key_digest = hashlib.sha256(execution_key.encode("utf-8")).hexdigest()
        return self.path.parent / f"{self.path.name}.launch-inputs" / key_digest

    def _retained_verified_inputs(self, request: SessionRequest, execution_key: str) -> VerifiedLaunchInputs:
        root = self._retained_snapshot_root(execution_key)
        image_snapshot = root / "image"
        self._verify_file(image_snapshot, request.image_digest)
        staged_paths: dict[str, Path] = {}
        for index, identity in enumerate(request.staged_inputs):
            snapshot = root / "staged" / str(index)
            self._verify_file(snapshot, identity.transfer_digest, identity.size_bytes)
            staged_paths[identity.logical_name] = snapshot
        return VerifiedLaunchInputs(image_path=image_snapshot, staged_paths=staged_paths)

    def _snapshot_verified_inputs(self, request: SessionRequest, execution_key: str) -> tuple[Path, VerifiedLaunchInputs]:
        final_root = self._retained_snapshot_root(execution_key)
        final_root.parent.mkdir(parents=True, exist_ok=True)
        if final_root.exists():
            return final_root, self._retained_verified_inputs(request, execution_key)
        root = Path(tempfile.mkdtemp(prefix="fleet-launch-", dir=final_root.parent))
        try:
            image_snapshot = root / "image"
            image_digest, _ = self._snapshot_file(Path(self.image_path_resolver(request)), image_snapshot)
            if image_digest != request.image_digest:
                raise FleetPlacementError("Capsule image bytes do not match authorized image digest")
            staged_paths: dict[str, Path] = {}; staged_root = root / "staged"; staged_root.mkdir()
            for index, identity in enumerate(request.staged_inputs):
                snapshot = staged_root / str(index)
                digest, size = self._snapshot_file(Path(self.staged_path_resolver(identity)), snapshot)
                if digest != identity.transfer_digest or size != identity.size_bytes:
                    raise FleetPlacementError(f"staged input bytes do not match authorized identity: {identity.logical_name}")
                staged_paths[identity.logical_name] = snapshot
            root.replace(final_root)
            return final_root, VerifiedLaunchInputs(image_path=final_root / "image", staged_paths={name: final_root / "staged" / str(index) for index, name in enumerate(staged_paths)})
        except Exception:
            shutil.rmtree(root, ignore_errors=True); raise

    @staticmethod
    def _execution_key(request: SessionRequest, generation: int) -> str:
        return f"fleet:{request.session_request_id}:{generation}"

    @staticmethod
    def _state(row: sqlite3.Row) -> FleetExecutionState:
        return FleetExecutionState(row["session_request_id"], int(row["placement_generation"]), row["state"])

    def _repair_admission(self, session_request_id: str, generation: int, state: str) -> str:
        """Repair admission and return the effective durable outcome."""
        if state == "running":
            self.admission_store.transition(session_request_id, placement_generation=generation, new_state="running")
            return state
        if state in {"completed", "failed", "cancelled"}:
            try:
                self.admission_store.mark_terminal(session_request_id, placement_generation=generation, terminal_state=state)
                return state
            except FleetPlacementError:
                if state == "cancelled":
                    raise
                self.admission_store.mark_terminal(session_request_id, placement_generation=generation, terminal_state="cancelled")
                return "cancelled"
        return state

    def _record_reconciliation_intent(self, session_request_id: str, generation: int, observed: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT observed_state FROM fleet_execution_reconciliation WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, generation),
            ).fetchone()
            if row is not None and row["observed_state"] != observed:
                raise FleetPlacementError("execution reconciliation conflicts with durable observed state")
            conn.execute(
                "INSERT OR IGNORE INTO fleet_execution_reconciliation(session_request_id, placement_generation, observed_state) VALUES(?, ?, ?)",
                (session_request_id, generation, observed),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _pending_reconciliation(self, session_request_id: str, generation: int) -> Optional[str]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT observed_state FROM fleet_execution_reconciliation WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, generation),
            ).fetchone()
            return None if row is None else str(row["observed_state"])
        finally:
            conn.close()

    def _finish_reconciliation(self, session_request_id: str, generation: int, observed: str, execution_key: str) -> FleetExecutionState:
        effective = self._repair_admission(session_request_id, generation, observed)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE fleet_node_executions SET state=? WHERE session_request_id=? AND placement_generation=?",
                (effective, session_request_id, generation),
            )
            conn.execute(
                "DELETE FROM fleet_execution_reconciliation WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, generation),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        shutil.rmtree(self._retained_snapshot_root(execution_key), ignore_errors=True)
        return FleetExecutionState(session_request_id, generation, effective)

    def dispatch(self, request: SessionRequest, authorization: PlacementAuthorization, *, target: str) -> FleetExecutionState:
        admitted = self.admission_store.admit(request, authorization); generation = authorization.placement_generation
        if admitted.state in {"completed", "failed", "cancelled", "retained", "released"}:
            return FleetExecutionState(request.session_request_id, generation, admitted.state)
        conn = self._connect(); snapshot_root = None; verified_inputs = None
        execution_key = self._execution_key(request, generation); existing_state = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM fleet_node_executions WHERE session_request_id=? AND placement_generation=?", (request.session_request_id, generation)).fetchone()
            if row is not None:
                if row["run_id"] != str(request.run_id) or row["request_digest"] != request.request_digest:
                    raise FleetPlacementError("Fleet execution claim conflicts with authorized request")
                if row["execution_key"] != execution_key:
                    raise FleetPlacementError("Fleet execution key conflicts with authorized request")
                existing_state = str(row["state"])
                if existing_state in {"running", "completed", "failed", "cancelled"}:
                    conn.commit()
                    effective = self._repair_admission(request.session_request_id, generation, existing_state)
                    if effective != existing_state:
                        self._set_state(request.session_request_id, generation, effective)
                    return FleetExecutionState(request.session_request_id, generation, effective)
            try:
                snapshot_root, verified_inputs = self._snapshot_verified_inputs(request, execution_key)
            except Exception:
                if row is None:
                    self.admission_store.mark_terminal(request.session_request_id, placement_generation=generation, terminal_state="failed")
                raise
            if row is None:
                conn.execute("INSERT INTO fleet_node_executions(session_request_id, placement_generation, run_id, request_digest, execution_key, state) VALUES(?, ?, ?, ?, ?, 'prepared')", (request.session_request_id, generation, str(request.run_id), request.request_digest, execution_key))
            conn.commit()
        except Exception:
            conn.rollback()
            if existing_state is None and snapshot_root is not None: shutil.rmtree(snapshot_root, ignore_errors=True)
            raise
        finally:
            conn.close()
        assert verified_inputs is not None and snapshot_root is not None
        environment = None; launch_attempted = existing_state == "launching"; completed_launch = False
        try:
            environment = self.environment_factory(request, verified_inputs, execution_key)
            if not isinstance(environment, ExecutionEnvironment) or environment.environment_type != "capsule":
                raise FleetPlacementError("Fleet execution requires a Capsule ExecutionEnvironment; local fallback is forbidden")
            if admitted.state == "reserved":
                self.admission_store.transition(request.session_request_id, placement_generation=generation, new_state="allocated")
                admitted = self.admission_store.admit(request, authorization)
            if admitted.state == "allocated": self.admission_store.transition(request.session_request_id, placement_generation=generation, new_state="starting")
            self._set_state(request.session_request_id, generation, "launching"); launch_attempted = True
            environment.launch(target)
            self.admission_store.transition(request.session_request_id, placement_generation=generation, new_state="running")
            self._set_state(request.session_request_id, generation, "running"); completed_launch = True
            return FleetExecutionState(request.session_request_id, generation, "running")
        except Exception:
            if launch_attempted: raise
            self._set_state(request.session_request_id, generation, "failed")
            try: self.admission_store.mark_terminal(request.session_request_id, placement_generation=generation, terminal_state="failed")
            except FleetPlacementError: pass
            if environment is not None:
                try: environment.close()
                except Exception: pass
            raise
        finally:
            if completed_launch or not launch_attempted: shutil.rmtree(snapshot_root, ignore_errors=True)

    def _set_state(self, session_request_id: str, generation: int, state: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE fleet_node_executions SET state=? WHERE session_request_id=? AND placement_generation=?", (state, session_request_id, generation))
            conn.commit()
        finally: conn.close()

    def reconcile(self, session_request_id: str, *, placement_generation: int) -> FleetExecutionState:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM fleet_node_executions WHERE session_request_id=? AND placement_generation=?", (session_request_id, placement_generation)).fetchone()
        finally: conn.close()
        if row is None: raise FleetPlacementError("Fleet execution claim is unknown")
        current = self._state(row); execution_key = str(row["execution_key"])

        pending = self._pending_reconciliation(session_request_id, placement_generation)
        if pending is not None:
            return self._finish_reconciliation(session_request_id, placement_generation, pending, execution_key)

        if current.state in {"running", "completed", "failed", "cancelled"}:
            effective = self._repair_admission(session_request_id, placement_generation, current.state)
            if effective != current.state:
                self._set_state(session_request_id, placement_generation, effective)
                current = FleetExecutionState(session_request_id, placement_generation, effective)
            shutil.rmtree(self._retained_snapshot_root(execution_key), ignore_errors=True)
            if current.state != "running" or self.execution_probe is None: return current
        elif current.state != "launching" or self.execution_probe is None:
            return current
        observed = self.execution_probe(execution_key)
        if observed is None: return current
        if observed not in {"running", "completed", "failed", "cancelled"}:
            raise FleetPlacementError("execution reconciliation returned an invalid state")

        # Persist the provider observation before touching the independent admission
        # database. The intent is replayable after a crash in either write order,
        # so restart never depends on the provider retaining a terminal execution.
        self._record_reconciliation_intent(session_request_id, placement_generation, observed)
        return self._finish_reconciliation(session_request_id, placement_generation, observed, execution_key)
