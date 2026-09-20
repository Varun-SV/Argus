"""Canonical ATES transport/aggregation for Argus Fleet.

Fleet never rewrites producer evidence. The Node sends the exact StoredEvent
selected from its canonical AtesEventStore. The Control Center mirrors those
same event bytes through AtesEventStore.append_event(), while transport-only
metadata (Node identity, placement generation, trusted receipt time, and clock
assessment) is kept in a separate SQLite index.

The transaction is intentionally recoverable rather than pretending the
filesystem ATES append and SQLite receipt index are one atomic store:
a receipt is first recorded as pending, the canonical event is appended, then
the receipt is committed. A crash between those steps is repaired by replaying
the exact same event; AtesEventStore already makes exact replay idempotent and
conflicting event-id/sequence content fail closed.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence

from argus.ates import AtesEventConflict, AtesEventStore, RunId, StoredEvent

from .placement import PlacementRecord

FLEET_ATES_TRANSPORT_VERSION = "argus-fleet-ates-transport-v1"
_NODE_RE = re.compile(r"^NODE-[0-9a-f]{32}$")
_SESSION_RE = re.compile(r"^SESSION-[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class FleetAtesError(RuntimeError):
    """Fleet ATES transport state cannot be trusted or reconciled safely."""


class FleetAtesConflict(FleetAtesError):
    """Transport identity/content conflicts with already accepted state."""


class FleetAtesGap(FleetAtesError):
    """A canonical event arrived ahead of a missing sequence."""

    def __init__(self, *, run_id: str, expected_sequence: int, received_sequence: int):
        super().__init__(
            f"Fleet ATES sequence gap for {run_id}: expected "
            f"{expected_sequence}, got {received_sequence}"
        )
        self.run_id = run_id
        self.expected_sequence = expected_sequence
        self.received_sequence = received_sequence


def _clean(value: str, label: str, *, max_length: int = 255) -> str:
    if not isinstance(value, str):
        raise FleetAtesError(f"{label} must be a string")
    cleaned = value.strip()
    if not cleaned or cleaned != value or len(cleaned) > max_length:
        raise FleetAtesError(f"{label} must be canonical and non-empty")
    return cleaned


def _finite_nonnegative(value: float, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise FleetAtesError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise FleetAtesError(f"{label} must be finite and non-negative")
    return result


def _sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise FleetAtesError(f"{label} must be lowercase sha256:<64-hex>")
    return value


def _event_digest(event: StoredEvent) -> str:
    return "sha256:" + hashlib.sha256(event.canonical_line()).hexdigest()


@dataclass(frozen=True)
class FleetAtesRunBinding:
    run_id: RunId
    session_request_id: str
    node_id: str
    placement_generation: int
    placement_request_digest: str
    image_digest: str

    @classmethod
    def from_placement(
        cls,
        placement: PlacementRecord,
        *,
        image_digest: str,
    ) -> "FleetAtesRunBinding":
        if not isinstance(placement, PlacementRecord):
            raise FleetAtesError("ATES binding requires an authoritative PlacementRecord")
        return cls(
            run_id=RunId(placement.run_id),
            session_request_id=placement.session_request_id,
            node_id=placement.owner_node_id,
            placement_generation=placement.placement_generation,
            placement_request_digest=placement.request_digest,
            image_digest=image_digest,
        )

    def __post_init__(self) -> None:
        try:
            RunId(str(self.run_id))
        except (TypeError, ValueError) as exc:
            raise FleetAtesError("invalid ATES run id") from exc
        if not _SESSION_RE.fullmatch(self.session_request_id):
            raise FleetAtesError("invalid session request id")
        if not _NODE_RE.fullmatch(self.node_id):
            raise FleetAtesError("invalid Node id")
        if (
            not isinstance(self.placement_generation, int)
            or isinstance(self.placement_generation, bool)
            or self.placement_generation < 1
        ):
            raise FleetAtesError("placement generation must be a positive integer")
        _sha256(self.placement_request_digest, "placement request digest")
        _sha256(self.image_digest, "Capsule image digest")


@dataclass(frozen=True)
class FleetAtesReceipt:
    run_id: str
    sequence: int
    event_id: str
    event_digest: str
    node_id: str
    placement_generation: int
    first_received_at: float
    state: str
    clock_offset_ms: Optional[float]
    clock_uncertainty_ms: Optional[float]
    clock_sample_age_ms: Optional[float]


@dataclass(frozen=True)
class FleetAtesBatch:
    run_id: RunId
    events: tuple[StoredEvent, ...]

    @classmethod
    def from_store(
        cls,
        store: AtesEventStore,
        *,
        after_sequence: int = 0,
        limit: int = 256,
    ) -> "FleetAtesBatch":
        if not isinstance(store, AtesEventStore):
            raise FleetAtesError("Fleet ATES batch source must be an AtesEventStore")
        if not isinstance(after_sequence, int) or isinstance(after_sequence, bool) or after_sequence < 0:
            raise FleetAtesError("after_sequence must be a non-negative integer")
        if not isinstance(limit, int) or isinstance(limit, bool) or not (1 <= limit <= 4096):
            raise FleetAtesError("ATES batch limit must be between 1 and 4096")
        selected = tuple(
            event
            for event in store.events
            if event.sequence > after_sequence
        )[:limit]
        return cls(run_id=store.run_id, events=selected)

    @classmethod
    def from_events(
        cls,
        run_id: RunId | str,
        events: Sequence[StoredEvent],
    ) -> "FleetAtesBatch":
        rid = run_id if isinstance(run_id, RunId) else RunId(run_id)
        frozen = tuple(events)
        for event in frozen:
            if not isinstance(event, StoredEvent) or event.run_id != rid:
                raise FleetAtesError("ATES batch contains an event for another run")
        if any(
            frozen[index].sequence >= frozen[index + 1].sequence
            for index in range(len(frozen) - 1)
        ):
            raise FleetAtesError("ATES batch events must be strictly sequence ordered")
        return cls(run_id=rid, events=frozen)


class FleetAtesAggregator:
    """Crash-recoverable Control Center mirror of canonical Node ATES streams."""

    def __init__(
        self,
        index_path: Path | str,
        *,
        mirror_project_dir: Path | str,
        control_center_id: str,
    ) -> None:
        self.index_path = Path(index_path)
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.mirror_project_dir = Path(mirror_project_dir)
        self.mirror_project_dir.mkdir(parents=True, exist_ok=True)
        self.control_center_id = _clean(control_center_id, "control center id")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.index_path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS fleet_ates_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fleet_ates_runs (
                    run_id TEXT PRIMARY KEY,
                    session_request_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    placement_generation INTEGER NOT NULL,
                    placement_request_digest TEXT NOT NULL,
                    image_digest TEXT NOT NULL,
                    bound_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fleet_ates_receipts (
                    run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    event_id TEXT NOT NULL,
                    event_digest TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    placement_generation INTEGER NOT NULL,
                    first_received_at REAL NOT NULL,
                    state TEXT NOT NULL,
                    clock_offset_ms REAL,
                    clock_uncertainty_ms REAL,
                    clock_sample_age_ms REAL,
                    PRIMARY KEY(run_id, sequence),
                    UNIQUE(run_id, event_id)
                );
                """
            )
            expected = {
                "transport_version": FLEET_ATES_TRANSPORT_VERSION,
                "control_center_id": self.control_center_id,
            }
            for key, value in expected.items():
                row = conn.execute(
                    "SELECT value FROM fleet_ates_meta WHERE key=?",
                    (key,),
                ).fetchone()
                if row is None:
                    conn.execute(
                        "INSERT INTO fleet_ates_meta(key, value) VALUES(?, ?)",
                        (key, value),
                    )
                elif row["value"] != value:
                    raise FleetAtesError(
                        f"Fleet ATES index {key} does not match configuration"
                    )
        except sqlite3.Error as exc:
            raise FleetAtesError("cannot initialize Fleet ATES index") from exc
        finally:
            conn.close()

    @staticmethod
    def _binding_tuple(binding: FleetAtesRunBinding) -> tuple[object, ...]:
        return (
            str(binding.run_id),
            binding.session_request_id,
            binding.node_id,
            binding.placement_generation,
            binding.placement_request_digest,
            binding.image_digest,
        )

    def bind_run(
        self,
        binding: FleetAtesRunBinding,
        *,
        bound_at: Optional[float] = None,
    ) -> None:
        if not isinstance(binding, FleetAtesRunBinding):
            raise FleetAtesError("binding must be FleetAtesRunBinding")
        current = _finite_nonnegative(
            time.time() if bound_at is None else bound_at,
            "ATES binding time",
        )
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM fleet_ates_runs WHERE run_id=?",
                (str(binding.run_id),),
            ).fetchone()
            if row is not None:
                existing = (
                    row["run_id"],
                    row["session_request_id"],
                    row["node_id"],
                    int(row["placement_generation"]),
                    row["placement_request_digest"],
                    row["image_digest"],
                )
                if existing != self._binding_tuple(binding):
                    raise FleetAtesConflict(
                        "ATES run is already bound to different Fleet provenance"
                    )
                conn.commit()
                return
            conn.execute(
                """
                INSERT INTO fleet_ates_runs(
                    run_id, session_request_id, node_id, placement_generation,
                    placement_request_digest, image_digest, bound_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (*self._binding_tuple(binding), current),
            )
            conn.commit()
        except FleetAtesError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetAtesError("cannot persist Fleet ATES run binding") from exc
        finally:
            conn.close()

    def _require_binding(
        self,
        conn: sqlite3.Connection,
        binding: FleetAtesRunBinding,
    ) -> None:
        row = conn.execute(
            "SELECT * FROM fleet_ates_runs WHERE run_id=?",
            (str(binding.run_id),),
        ).fetchone()
        if row is None:
            raise FleetAtesError("ATES run has not been bound to a Fleet placement")
        existing = (
            row["run_id"],
            row["session_request_id"],
            row["node_id"],
            int(row["placement_generation"]),
            row["placement_request_digest"],
            row["image_digest"],
        )
        if existing != self._binding_tuple(binding):
            raise FleetAtesConflict(
                "Fleet ATES event provenance does not match the bound run"
            )

    @staticmethod
    def _clock_values(
        clock_assessment: Optional[object],
    ) -> tuple[Optional[float], Optional[float], Optional[float]]:
        if clock_assessment is None:
            return None, None, None
        try:
            offset = float(getattr(clock_assessment, "node_minus_control_offset_ms"))
            uncertainty = float(getattr(clock_assessment, "uncertainty_ms"))
            age = float(getattr(clock_assessment, "sample_age_ms"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise FleetAtesError("invalid Fleet clock assessment") from exc
        for value, label in (
            (abs(offset), "clock offset"),
            (uncertainty, "clock uncertainty"),
            (age, "clock sample age"),
        ):
            _finite_nonnegative(value, label)
        return offset, uncertainty, age

    @staticmethod
    def _receipt(row: sqlite3.Row) -> FleetAtesReceipt:
        return FleetAtesReceipt(
            run_id=row["run_id"],
            sequence=int(row["sequence"]),
            event_id=row["event_id"],
            event_digest=row["event_digest"],
            node_id=row["node_id"],
            placement_generation=int(row["placement_generation"]),
            first_received_at=float(row["first_received_at"]),
            state=row["state"],
            clock_offset_ms=(
                float(row["clock_offset_ms"])
                if row["clock_offset_ms"] is not None
                else None
            ),
            clock_uncertainty_ms=(
                float(row["clock_uncertainty_ms"])
                if row["clock_uncertainty_ms"] is not None
                else None
            ),
            clock_sample_age_ms=(
                float(row["clock_sample_age_ms"])
                if row["clock_sample_age_ms"] is not None
                else None
            ),
        )

    def ingest_event(
        self,
        binding: FleetAtesRunBinding,
        event: StoredEvent,
        *,
        received_at: Optional[float] = None,
        clock_assessment: Optional[object] = None,
    ) -> FleetAtesReceipt:
        if not isinstance(event, StoredEvent):
            raise FleetAtesError("Fleet ATES transport accepts only StoredEvent")
        if event.run_id != binding.run_id:
            raise FleetAtesConflict("event run_id does not match Fleet run binding")
        received = _finite_nonnegative(
            time.time() if received_at is None else received_at,
            "Control Center ATES receipt time",
        )
        clock_offset, clock_uncertainty, clock_age = self._clock_values(
            clock_assessment
        )
        digest = _event_digest(event)
        inserted_pending = False

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._require_binding(conn, binding)
            row = conn.execute(
                """
                SELECT * FROM fleet_ates_receipts
                 WHERE run_id=? AND sequence=?
                """,
                (str(binding.run_id), event.sequence),
            ).fetchone()
            if row is None:
                by_id = conn.execute(
                    """
                    SELECT * FROM fleet_ates_receipts
                     WHERE run_id=? AND event_id=?
                    """,
                    (str(binding.run_id), str(event.event_id)),
                ).fetchone()
                if by_id is not None:
                    raise FleetAtesConflict(
                        "event_id is already bound to another ATES sequence"
                    )
                conn.execute(
                    """
                    INSERT INTO fleet_ates_receipts(
                        run_id, sequence, event_id, event_digest, node_id,
                        placement_generation, first_received_at, state,
                        clock_offset_ms, clock_uncertainty_ms,
                        clock_sample_age_ms
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                    """,
                    (
                        str(binding.run_id),
                        event.sequence,
                        str(event.event_id),
                        digest,
                        binding.node_id,
                        binding.placement_generation,
                        received,
                        clock_offset,
                        clock_uncertainty,
                        clock_age,
                    ),
                )
                inserted_pending = True
            else:
                if (
                    row["event_id"] != str(event.event_id)
                    or row["event_digest"] != digest
                    or row["node_id"] != binding.node_id
                    or int(row["placement_generation"])
                    != binding.placement_generation
                ):
                    raise FleetAtesConflict(
                        "ATES sequence already has conflicting transport content"
                    )
                if row["state"] == "committed":
                    conn.commit()
                    return self._receipt(row)
            conn.commit()
        except FleetAtesError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetAtesError("cannot persist pending ATES receipt") from exc
        finally:
            conn.close()

        try:
            with AtesEventStore(self.mirror_project_dir, binding.run_id) as mirror:
                expected = mirror.next_sequence
                if event.sequence > expected:
                    raise FleetAtesGap(
                        run_id=str(binding.run_id),
                        expected_sequence=expected,
                        received_sequence=event.sequence,
                    )
                mirror.append_event(event)
        except FleetAtesGap:
            # Keep the exact pending receipt. Once the missing sequence arrives,
            # replaying this event preserves its original trusted receipt time.
            raise
        except AtesEventConflict as exc:
            raise FleetAtesConflict(
                f"canonical ATES mirror rejected conflicting event: {exc}"
            ) from exc
        except Exception as exc:
            # A local I/O failure is retryable with this same event identity.
            raise FleetAtesError(
                f"canonical ATES mirror append failed: {type(exc).__name__}: {exc}"
            ) from exc

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._require_binding(conn, binding)
            row = conn.execute(
                """
                SELECT * FROM fleet_ates_receipts
                 WHERE run_id=? AND sequence=?
                """,
                (str(binding.run_id), event.sequence),
            ).fetchone()
            if row is None:
                raise FleetAtesError(
                    "pending ATES receipt disappeared after canonical append"
                )
            if row["event_id"] != str(event.event_id) or row["event_digest"] != digest:
                raise FleetAtesConflict(
                    "pending ATES receipt changed after canonical append"
                )
            conn.execute(
                """
                UPDATE fleet_ates_receipts
                   SET state='committed'
                 WHERE run_id=? AND sequence=?
                """,
                (str(binding.run_id), event.sequence),
            )
            committed = conn.execute(
                """
                SELECT * FROM fleet_ates_receipts
                 WHERE run_id=? AND sequence=?
                """,
                (str(binding.run_id), event.sequence),
            ).fetchone()
            conn.commit()
            return self._receipt(committed)
        except FleetAtesError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetAtesError(
                "canonical ATES event was mirrored but receipt commit failed; "
                "replay the exact event to reconcile"
            ) from exc
        finally:
            conn.close()

    def ingest_batch(
        self,
        binding: FleetAtesRunBinding,
        batch: FleetAtesBatch,
        *,
        received_at: Optional[float] = None,
        clock_assessment: Optional[object] = None,
    ) -> tuple[FleetAtesReceipt, ...]:
        if batch.run_id != binding.run_id:
            raise FleetAtesConflict("ATES batch run_id does not match Fleet binding")
        base_received = _finite_nonnegative(
            time.time() if received_at is None else received_at,
            "Control Center ATES batch receipt time",
        )
        receipts: list[FleetAtesReceipt] = []
        for event in batch.events:
            receipts.append(
                self.ingest_event(
                    binding,
                    event,
                    received_at=base_received,
                    clock_assessment=clock_assessment,
                )
            )
        return tuple(receipts)

    def committed_sequence(self, run_id: RunId | str) -> int:
        rid = run_id if isinstance(run_id, RunId) else RunId(run_id)
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT sequence FROM fleet_ates_receipts
                 WHERE run_id=? AND state='committed'
                 ORDER BY sequence
                """,
                (str(rid),),
            ).fetchall()
            expected = 1
            for row in rows:
                sequence = int(row["sequence"])
                if sequence != expected:
                    break
                expected += 1
            return expected - 1
        finally:
            conn.close()

    def receipts(self, run_id: RunId | str) -> tuple[FleetAtesReceipt, ...]:
        rid = run_id if isinstance(run_id, RunId) else RunId(run_id)
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM fleet_ates_receipts
                 WHERE run_id=?
                 ORDER BY sequence
                """,
                (str(rid),),
            ).fetchall()
            return tuple(self._receipt(row) for row in rows)
        finally:
            conn.close()
