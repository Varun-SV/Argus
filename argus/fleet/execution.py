"""Node-side Fleet bridge into the existing Capsule ExecutionEnvironment.

This module owns the mutation boundary between a durable Fleet admission and one
Capsule launch.  It deliberately has no local-execution fallback: a Fleet
placement either launches through an existing Capsule ExecutionEnvironment or
fails closed.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional

from argus.execution.base import ExecutionEnvironment

from .placement import (
    FleetPlacementError,
    NodeAdmissionStore,
    PlacementAuthorization,
    SessionRequest,
    StagedInputIdentity,
)


@dataclass(frozen=True)
class FleetExecutionState:
    session_request_id: str
    placement_generation: int
    state: str


class FleetNodeExecutor:
    """Idempotently translate one authorized Fleet placement into a Capsule.

    ``environment_factory`` must return the existing Capsule execution
    abstraction.  ``execution_probe`` is the provider/agent reconciliation hook
    used after process restart; it reports ``running``, ``completed``,
    ``failed``, or ``cancelled`` for the stable Fleet execution key.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        admission_store: NodeAdmissionStore,
        environment_factory: Callable[[SessionRequest], ExecutionEnvironment],
        image_path_resolver: Callable[[SessionRequest], Path | str],
        staged_path_resolver: Callable[[StagedInputIdentity], Path | str],
        execution_probe: Optional[Callable[[str], Optional[str]]] = None,
    ) -> None:
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fleet_node_executions (
                    session_request_id TEXT NOT NULL,
                    placement_generation INTEGER NOT NULL,
                    run_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    execution_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    PRIMARY KEY(session_request_id, placement_generation)
                )
                """
            )
        finally:
            conn.close()

    @staticmethod
    def _digest_file(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as handle:
                while True:
                    block = handle.read(1024 * 1024)
                    if not block:
                        break
                    size += len(block)
                    digest.update(block)
        except OSError as exc:
            raise FleetPlacementError(f"cannot verify Fleet launch input: {path}") from exc
        return "sha256:" + digest.hexdigest(), size

    def _verify_launch_inputs(self, request: SessionRequest) -> None:
        image = Path(self.image_path_resolver(request))
        image_digest, _ = self._digest_file(image)
        if image_digest != request.image_digest:
            raise FleetPlacementError("Capsule image bytes do not match authorized image digest")
        for identity in request.staged_inputs:
            source = Path(self.staged_path_resolver(identity))
            digest, size = self._digest_file(source)
            if digest != identity.transfer_digest or size != identity.size_bytes:
                raise FleetPlacementError(
                    f"staged input bytes do not match authorized identity: {identity.logical_name}"
                )

    @staticmethod
    def _execution_key(request: SessionRequest, generation: int) -> str:
        return f"fleet:{request.session_request_id}:{generation}"

    @staticmethod
    def _state(row: sqlite3.Row) -> FleetExecutionState:
        return FleetExecutionState(
            session_request_id=row["session_request_id"],
            placement_generation=int(row["placement_generation"]),
            state=row["state"],
        )

    def dispatch(
        self,
        request: SessionRequest,
        authorization: PlacementAuthorization,
        *,
        target: str,
    ) -> FleetExecutionState:
        """Admit and launch exactly once for this placement generation.

        The durable ``launching`` claim is committed before the Capsule API is
        called.  Therefore a lost response or Node-agent restart can never turn
        a retry into a second launch.  Ambiguous claims are reconciled instead
        of retried.
        """
        admitted = self.admission_store.admit(request, authorization)
        generation = authorization.placement_generation
        if admitted.state in {"completed", "failed", "cancelled", "retained", "released"}:
            return FleetExecutionState(request.session_request_id, generation, admitted.state)

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM fleet_node_executions WHERE session_request_id=? AND placement_generation=?",
                (request.session_request_id, generation),
            ).fetchone()
            if row is not None:
                if row["run_id"] != str(request.run_id) or row["request_digest"] != request.request_digest:
                    raise FleetPlacementError("Fleet execution claim conflicts with authorized request")
                conn.commit()
                return self._state(row)

            self._verify_launch_inputs(request)
            execution_key = self._execution_key(request, generation)
            conn.execute(
                """
                INSERT INTO fleet_node_executions(
                    session_request_id, placement_generation, run_id,
                    request_digest, execution_key, state
                ) VALUES(?, ?, ?, ?, ?, 'launching')
                """,
                (
                    request.session_request_id,
                    generation,
                    str(request.run_id),
                    request.request_digest,
                    execution_key,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        # The claim is durable before any Capsule side effect.  Do not move this
        # above the commit: doing so re-opens duplicate launch after lost reply.
        environment: Optional[ExecutionEnvironment] = None
        try:
            environment = self.environment_factory(request)
            if not isinstance(environment, ExecutionEnvironment) or environment.environment_type != "capsule":
                raise FleetPlacementError("Fleet execution requires a Capsule ExecutionEnvironment; local fallback is forbidden")
            self.admission_store.transition(
                request.session_request_id,
                placement_generation=generation,
                new_state="allocated",
            )
            self.admission_store.transition(
                request.session_request_id,
                placement_generation=generation,
                new_state="starting",
            )
            environment.launch(target)
            self.admission_store.transition(
                request.session_request_id,
                placement_generation=generation,
                new_state="running",
            )
            self._set_state(request.session_request_id, generation, "running")
            return FleetExecutionState(request.session_request_id, generation, "running")
        except Exception:
            self._set_state(request.session_request_id, generation, "failed")
            try:
                self.admission_store.mark_terminal(
                    request.session_request_id,
                    placement_generation=generation,
                    terminal_state="failed",
                )
            except FleetPlacementError:
                pass
            if environment is not None:
                try:
                    environment.close()
                except Exception:
                    pass
            raise

    def _set_state(self, session_request_id: str, generation: int, state: str) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE fleet_node_executions SET state=? WHERE session_request_id=? AND placement_generation=?",
                (state, session_request_id, generation),
            )
            conn.commit()
        finally:
            conn.close()

    def reconcile(self, session_request_id: str, *, placement_generation: int) -> FleetExecutionState:
        """Reconcile an ambiguous launch without ever launching a replacement."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM fleet_node_executions WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, placement_generation),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise FleetPlacementError("Fleet execution claim is unknown")
        current = self._state(row)
        if current.state not in {"launching", "running"} or self.execution_probe is None:
            return current
        observed = self.execution_probe(row["execution_key"])
        if observed is None:
            return current
        if observed not in {"running", "completed", "failed", "cancelled"}:
            raise FleetPlacementError("execution reconciliation returned an invalid state")
        self._set_state(session_request_id, placement_generation, observed)
        if observed in {"completed", "failed", "cancelled"}:
            self.admission_store.mark_terminal(
                session_request_id,
                placement_generation=placement_generation,
                terminal_state=observed,
            )
        return FleetExecutionState(session_request_id, placement_generation, observed)
