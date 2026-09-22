"""Durable Fleet placement, fencing, cancellation, and Node admission.

The Control Center store owns global placement generation and cancellation
truth.  The Node admission store owns local capacity and side-effect admission.
Neither heartbeat loss nor lease expiry appears in this module as a fencing
primitive: ownership moves only after explicit terminal or definitive-fence
state is durably recorded.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from argus.ates import RunId

from .identity import (
    ControlCenterKeyPair,
    FleetIdentityError,
    canonical_signed_message,
    public_key_fingerprint,
    verify_signature,
)

PLACEMENT_VERSION = "argus-fleet-placement-v1"
PLACEMENT_AUTH_VERSION = "argus-fleet-placement-auth-v1"
RETIREMENT_AUTH_VERSION = "argus-fleet-generation-retirement-v1"
_SESSION_RE = re.compile(r"^SESSION-[A-Za-z0-9][A-Za-z0-9_-]{0,95}$")
_NODE_RE = re.compile(r"^NODE-[0-9a-f]{32}$")
_OPERATION_RE = re.compile(r"^(?:DISPATCH|CANCEL|FENCE)-[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_TERMINAL_STATES = frozenset({"completed", "failed", "cancelled", "retained", "released"})
_NODE_ACTIVE_STATES = frozenset({"reserved", "allocated", "starting", "running", "cancellation_pending"})
_PROVENANCE_METHODS = frozenset({"sha256", "hmac-sha256", "opaque", "redacted-sha256"})
_FENCE_METHODS = frozenset({"node_terminal_tombstone", "provider_termination_and_tombstone", "host_execution_disabled"})


class FleetPlacementError(RuntimeError):
    """Placement state cannot be safely created, replayed, or advanced."""


class PlacementConflict(FleetPlacementError):
    """A stable Fleet identity was reused with conflicting immutable content."""


class CapacityUnavailable(FleetPlacementError):
    """The Node cannot atomically reserve the requested local capacity."""


def _clean(value: str, label: str, *, max_length: int = 255) -> str:
    if not isinstance(value, str):
        raise FleetPlacementError(f"{label} must be a string")
    cleaned = value.strip()
    if not cleaned or cleaned != value or len(cleaned) > max_length:
        raise FleetPlacementError(f"{label} must be canonical and non-empty")
    return cleaned


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise FleetPlacementError("placement value is not canonical JSON") from exc


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _sha256(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise FleetPlacementError(f"{label} must be lowercase sha256:<64-hex>")
    return value


def _positive_int(value: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise FleetPlacementError(f"{label} must be a positive integer")
    return value


def _finite_time(value: float, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise FleetPlacementError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise FleetPlacementError(f"{label} must be finite and non-negative")
    return result


@dataclass(frozen=True)
class StagedInputIdentity:
    logical_name: str
    size_bytes: int
    transfer_digest: str
    provenance_method: str
    provenance_value: str
    sensitive: bool = False

    def __post_init__(self) -> None:
        _clean(self.logical_name, "staged input logical name", max_length=256)
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise FleetPlacementError("staged input size must be a non-negative integer")
        _sha256(self.transfer_digest, "staged input transfer digest")
        if self.provenance_method not in _PROVENANCE_METHODS:
            raise FleetPlacementError("unsupported staged input provenance method")
        _clean(self.provenance_value, "staged input provenance value", max_length=1024)
        if not isinstance(self.sensitive, bool):
            raise FleetPlacementError("staged input sensitive flag must be boolean")
        if self.sensitive and self.provenance_method == "sha256":
            raise FleetPlacementError(
                "sensitive staged input cannot expose an ordinary raw sha256 provenance identity"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "logical_name": self.logical_name,
            "size_bytes": self.size_bytes,
            "transfer_digest": self.transfer_digest,
            "provenance_method": self.provenance_method,
            "provenance_value": self.provenance_value,
            "sensitive": self.sensitive,
        }


@dataclass(frozen=True)
class SessionRequest:
    session_request_id: str
    run_id: RunId
    image_digest: str
    spec_digest: str
    provider: str
    guest_os: str
    staged_inputs: tuple[StagedInputIdentity, ...]
    network_policy_json: str
    isolation_policy_json: str
    requested_slots: int = 1

    def __post_init__(self) -> None:
        if not _SESSION_RE.fullmatch(self.session_request_id):
            raise FleetPlacementError("invalid session request id")
        try:
            RunId(str(self.run_id))
        except (TypeError, ValueError) as exc:
            raise FleetPlacementError("invalid ATES run id") from exc
        _sha256(self.image_digest, "Capsule image digest")
        _sha256(self.spec_digest, "test specification digest")
        _clean(self.provider, "Capsule provider", max_length=64)
        _clean(self.guest_os, "guest os", max_length=64)
        _positive_int(self.requested_slots, "requested session slots")
        names: set[str] = set()
        for item in self.staged_inputs:
            if not isinstance(item, StagedInputIdentity):
                raise FleetPlacementError("staged inputs must use StagedInputIdentity")
            if item.logical_name in names:
                raise FleetPlacementError("staged input logical names must be unique")
            names.add(item.logical_name)
        for field_name in ("network_policy_json", "isolation_policy_json"):
            raw = getattr(self, field_name)
            if not isinstance(raw, str):
                raise FleetPlacementError(f"{field_name} must be canonical JSON text")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise FleetPlacementError(f"{field_name} is invalid JSON") from exc
            if not isinstance(parsed, dict) or _canonical(parsed).decode("utf-8") != raw:
                raise FleetPlacementError(f"{field_name} must be a canonical JSON object")

    @classmethod
    def create(
        cls,
        *,
        run_id: RunId,
        image_digest: str,
        spec_digest: str,
        provider: str,
        guest_os: str,
        staged_inputs: Sequence[StagedInputIdentity] = (),
        network_policy: Optional[Mapping[str, object]] = None,
        isolation_policy: Optional[Mapping[str, object]] = None,
        requested_slots: int = 1,
        session_request_id: Optional[str] = None,
    ) -> "SessionRequest":
        network = dict(network_policy or {})
        isolation = dict(isolation_policy or {})
        return cls(
            session_request_id=session_request_id or ("SESSION-" + uuid.uuid4().hex),
            run_id=RunId(str(run_id)),
            image_digest=image_digest,
            spec_digest=spec_digest,
            provider=provider,
            guest_os=guest_os,
            staged_inputs=tuple(staged_inputs),
            network_policy_json=_canonical(network).decode("utf-8"),
            isolation_policy_json=_canonical(isolation).decode("utf-8"),
            requested_slots=requested_slots,
        )

    @property
    def network_policy(self) -> dict[str, object]:
        return json.loads(self.network_policy_json)

    @property
    def isolation_policy(self) -> dict[str, object]:
        return json.loads(self.isolation_policy_json)

    def payload(self) -> dict[str, object]:
        return {
            "placement_version": PLACEMENT_VERSION,
            "session_request_id": self.session_request_id,
            "run_id": str(self.run_id),
            "image_digest": self.image_digest,
            "spec_digest": self.spec_digest,
            "provider": self.provider,
            "guest_os": self.guest_os,
            "staged_inputs": [item.to_dict() for item in self.staged_inputs],
            "network_policy": json.loads(self.network_policy_json),
            "isolation_policy": json.loads(self.isolation_policy_json),
            "requested_slots": self.requested_slots,
        }

    @property
    def request_digest(self) -> str:
        return _digest(self.payload())


@dataclass(frozen=True)
class PlacementRecord:
    session_request_id: str
    run_id: str
    request_digest: str
    owner_node_id: str
    placement_generation: int
    state: str
    dispatch_operation_id: str
    cancellation_state: str
    cancellation_operation_id: Optional[str]


@dataclass(frozen=True)
class PlacementAuthorization:
    session_request_id: str
    run_id: str
    request_digest: str
    owner_node_id: str
    placement_generation: int
    dispatch_operation_id: str
    control_center_id: str
    signer_public_key_b64: str
    signature: str

    @classmethod
    def create(
        cls,
        *,
        record: PlacementRecord,
        control_center_id: str,
        signer: ControlCenterKeyPair,
    ) -> "PlacementAuthorization":
        draft = cls(
            session_request_id=record.session_request_id,
            run_id=record.run_id,
            request_digest=record.request_digest,
            owner_node_id=record.owner_node_id,
            placement_generation=record.placement_generation,
            dispatch_operation_id=record.dispatch_operation_id,
            control_center_id=_clean(control_center_id, "control center id"),
            signer_public_key_b64=signer.public_key_b64,
            signature="",
        )
        signature = signer.sign(canonical_signed_message("fleet-placement-authorization", draft.payload()))
        return cls(**{**draft.__dict__, "signature": signature})

    def payload(self) -> dict[str, object]:
        return {
            "authorization_version": PLACEMENT_AUTH_VERSION,
            "session_request_id": self.session_request_id,
            "run_id": self.run_id,
            "request_digest": self.request_digest,
            "owner_node_id": self.owner_node_id,
            "placement_generation": self.placement_generation,
            "dispatch_operation_id": self.dispatch_operation_id,
            "control_center_id": self.control_center_id,
            "signer_public_key_b64": self.signer_public_key_b64,
        }

    def verify(self, expected_public_key_b64: str, *, control_center_id: str) -> None:
        if self.signer_public_key_b64 != expected_public_key_b64:
            raise FleetPlacementError("placement authorization signer is not the pinned Control Center")
        if self.control_center_id != control_center_id:
            raise FleetPlacementError("placement authorization audience mismatch")
        try:
            verify_signature(
                self.signer_public_key_b64,
                canonical_signed_message("fleet-placement-authorization", self.payload()),
                self.signature,
            )
        except FleetIdentityError as exc:
            raise FleetPlacementError("placement authorization signature is invalid") from exc


@dataclass(frozen=True)
class GenerationRetirementAuthorization:
    session_request_id: str
    placement_generation: int
    owner_node_id: str
    control_center_id: str
    signer_public_key_b64: str
    signature: str

    @classmethod
    def create(
        cls,
        *,
        session_request_id: str,
        placement_generation: int,
        owner_node_id: str,
        control_center_id: str,
        signer: ControlCenterKeyPair,
    ) -> "GenerationRetirementAuthorization":
        draft = cls(
            session_request_id=session_request_id,
            placement_generation=placement_generation,
            owner_node_id=owner_node_id,
            control_center_id=_clean(control_center_id, "control center id"),
            signer_public_key_b64=signer.public_key_b64,
            signature="",
        )
        signature = signer.sign(canonical_signed_message("fleet-generation-retirement", draft.payload()))
        return cls(**{**draft.__dict__, "signature": signature})

    def payload(self) -> dict[str, object]:
        return {
            "retirement_version": RETIREMENT_AUTH_VERSION,
            "session_request_id": self.session_request_id,
            "placement_generation": self.placement_generation,
            "owner_node_id": self.owner_node_id,
            "control_center_id": self.control_center_id,
            "signer_public_key_b64": self.signer_public_key_b64,
        }

    def verify(self, expected_public_key_b64: str, *, control_center_id: str) -> None:
        if self.signer_public_key_b64 != expected_public_key_b64:
            raise FleetPlacementError("generation retirement signer is not the pinned Control Center")
        if self.control_center_id != control_center_id:
            raise FleetPlacementError("generation retirement audience mismatch")
        try:
            verify_signature(
                self.signer_public_key_b64,
                canonical_signed_message("fleet-generation-retirement", self.payload()),
                self.signature,
            )
        except FleetIdentityError as exc:
            raise FleetPlacementError("generation retirement signature is invalid") from exc


class FleetPlacementStore:
    """Globally authoritative placement/cancellation state at the Control Center."""

    def __init__(self, path: Path | str, *, control_center_id: str) -> None:
        self.path = Path(path)
        self.control_center_id = _clean(control_center_id, "control center id")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    def _initialize(self) -> None:
        conn = self._connect()
        try:
            meta = conn.execute("SELECT value FROM fleet_meta WHERE key='control_center_id'").fetchone()
            if meta is None or meta["value"] != self.control_center_id:
                raise FleetPlacementError("placement store requires matching initialized Fleet registry")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS fleet_placements (
                    session_request_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    owner_node_id TEXT NOT NULL REFERENCES nodes(node_id),
                    placement_generation INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    dispatch_operation_id TEXT NOT NULL,
                    cancellation_state TEXT NOT NULL,
                    cancellation_operation_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fleet_placement_generations (
                    session_request_id TEXT NOT NULL,
                    placement_generation INTEGER NOT NULL,
                    owner_node_id TEXT NOT NULL REFERENCES nodes(node_id),
                    state TEXT NOT NULL,
                    fence_operation_id TEXT,
                    fence_method TEXT,
                    fence_evidence_digest TEXT,
                    retired_at REAL,
                    PRIMARY KEY(session_request_id, placement_generation)
                );
                CREATE TABLE IF NOT EXISTS fleet_cancellation_operations (
                    cancellation_operation_id TEXT PRIMARY KEY,
                    session_request_id TEXT NOT NULL,
                    placement_generation INTEGER NOT NULL,
                    requested_at REAL NOT NULL
                );
                """
            )
        except sqlite3.Error as exc:
            raise FleetPlacementError("cannot initialize Fleet placement store") from exc
        finally:
            conn.close()

    def _active_node(self, conn: sqlite3.Connection, node_id: str) -> None:
        if not _NODE_RE.fullmatch(node_id):
            raise FleetPlacementError("invalid owner node id")
        row = conn.execute("SELECT state FROM nodes WHERE node_id=?", (node_id,)).fetchone()
        if row is None or row["state"] != "active":
            raise FleetPlacementError("placement owner Node is not active/enrolled")

    @staticmethod
    def _record(row: sqlite3.Row) -> PlacementRecord:
        return PlacementRecord(
            session_request_id=row["session_request_id"],
            run_id=row["run_id"],
            request_digest=row["request_digest"],
            owner_node_id=row["owner_node_id"],
            placement_generation=int(row["placement_generation"]),
            state=row["state"],
            dispatch_operation_id=row["dispatch_operation_id"],
            cancellation_state=row["cancellation_state"],
            cancellation_operation_id=row["cancellation_operation_id"],
        )

    def create_placement(
        self,
        request: SessionRequest,
        *,
        owner_node_id: str,
        now: Optional[float] = None,
    ) -> PlacementRecord:
        current = _finite_time(time.time() if now is None else now, "placement time")
        payload_json = _canonical(request.payload()).decode("utf-8")
        digest = request.request_digest
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._active_node(conn, owner_node_id)
            existing = conn.execute(
                "SELECT * FROM fleet_placements WHERE session_request_id=?",
                (request.session_request_id,),
            ).fetchone()
            if existing is not None:
                if existing["run_id"] != str(request.run_id) or existing["request_digest"] != digest:
                    raise PlacementConflict("session request id was reused with different immutable request content")
                if existing["owner_node_id"] != owner_node_id:
                    raise PlacementConflict("existing placement owner cannot change without fencing/transfer")
                conn.commit()
                return self._record(existing)
            dispatch_id = "DISPATCH-" + uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO fleet_placements(
                    session_request_id, run_id, request_digest, request_json,
                    owner_node_id, placement_generation, state,
                    dispatch_operation_id, cancellation_state,
                    cancellation_operation_id, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 1, 'dispatching', ?, 'none', NULL, ?, ?)
                """,
                (
                    request.session_request_id,
                    str(request.run_id),
                    digest,
                    payload_json,
                    owner_node_id,
                    dispatch_id,
                    current,
                    current,
                ),
            )
            conn.execute(
                """
                INSERT INTO fleet_placement_generations(
                    session_request_id, placement_generation, owner_node_id, state
                ) VALUES(?, 1, ?, 'active')
                """,
                (request.session_request_id, owner_node_id),
            )
            row = conn.execute("SELECT * FROM fleet_placements WHERE session_request_id=?", (request.session_request_id,)).fetchone()
            conn.commit()
            return self._record(row)
        except FleetPlacementError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetPlacementError("placement transaction failed") from exc
        finally:
            conn.close()

    def get(self, session_request_id: str) -> PlacementRecord:
        if not _SESSION_RE.fullmatch(session_request_id):
            raise FleetPlacementError("invalid session request id")
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM fleet_placements WHERE session_request_id=?", (session_request_id,)).fetchone()
            if row is None:
                raise FleetPlacementError("placement is unknown")
            return self._record(row)
        finally:
            conn.close()

    def authorization_for_dispatch(
        self,
        session_request_id: str,
        *,
        signer: ControlCenterKeyPair,
    ) -> PlacementAuthorization:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM fleet_placements WHERE session_request_id=?", (session_request_id,)).fetchone()
            if row is None:
                raise FleetPlacementError("placement is unknown")
            if row["cancellation_state"] != "none" or row["state"] not in {"dispatching", "allocated", "starting"}:
                raise FleetPlacementError("placement is not start-dispatchable")
            generation = conn.execute(
                "SELECT state FROM fleet_placement_generations WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, row["placement_generation"]),
            ).fetchone()
            if generation is None or generation["state"] != "active":
                raise FleetPlacementError("placement generation is no longer executable")
            record = self._record(row)
            conn.commit()
            return PlacementAuthorization.create(record=record, control_center_id=self.control_center_id, signer=signer)
        except FleetPlacementError:
            conn.rollback()
            raise
        finally:
            conn.close()

    def request_cancellation(
        self,
        session_request_id: str,
        *,
        cancellation_operation_id: Optional[str] = None,
        now: Optional[float] = None,
    ) -> PlacementRecord:
        operation_id = cancellation_operation_id or ("CANCEL-" + uuid.uuid4().hex)
        if not _OPERATION_RE.fullmatch(operation_id) or not operation_id.startswith("CANCEL-"):
            raise FleetPlacementError("invalid cancellation operation id")
        current = _finite_time(time.time() if now is None else now, "cancellation time")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            used = conn.execute(
                "SELECT session_request_id, placement_generation FROM fleet_cancellation_operations WHERE cancellation_operation_id=?",
                (operation_id,),
            ).fetchone()
            row = conn.execute("SELECT * FROM fleet_placements WHERE session_request_id=?", (session_request_id,)).fetchone()
            if row is None:
                raise FleetPlacementError("placement is unknown")
            if used is not None and (
                used["session_request_id"] != session_request_id
                or int(used["placement_generation"]) != int(row["placement_generation"])
            ):
                raise PlacementConflict("cancellation operation id was reused for different placement")
            existing_op = row["cancellation_operation_id"]
            if existing_op is not None:
                if existing_op != operation_id:
                    raise PlacementConflict("placement already has a different cancellation operation")
                conn.commit()
                return self._record(row)
            if row["state"] in _TERMINAL_STATES:
                raise FleetPlacementError("terminal placement cannot accept a new cancellation")
            conn.execute(
                "INSERT INTO fleet_cancellation_operations(cancellation_operation_id, session_request_id, placement_generation, requested_at) VALUES(?, ?, ?, ?)",
                (operation_id, session_request_id, row["placement_generation"], current),
            )
            conn.execute(
                """
                UPDATE fleet_placements
                   SET state='cancellation_pending', cancellation_state='requested',
                       cancellation_operation_id=?, updated_at=?
                 WHERE session_request_id=?
                """,
                (operation_id, current, session_request_id),
            )
            updated = conn.execute("SELECT * FROM fleet_placements WHERE session_request_id=?", (session_request_id,)).fetchone()
            conn.commit()
            return self._record(updated)
        except FleetPlacementError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetPlacementError("cancellation transaction failed") from exc
        finally:
            conn.close()

    def mark_terminal(
        self,
        session_request_id: str,
        *,
        owner_node_id: str,
        placement_generation: int,
        terminal_state: str,
        now: Optional[float] = None,
    ) -> PlacementRecord:
        if terminal_state not in _TERMINAL_STATES:
            raise FleetPlacementError("invalid terminal placement state")
        current = _finite_time(time.time() if now is None else now, "terminal time")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM fleet_placements WHERE session_request_id=?", (session_request_id,)).fetchone()
            if row is None:
                raise FleetPlacementError("placement is unknown")
            if row["owner_node_id"] != owner_node_id or int(row["placement_generation"]) != placement_generation:
                raise PlacementConflict("terminal report does not match authoritative owner/generation")
            conn.execute(
                "UPDATE fleet_placements SET state=?, cancellation_state=CASE WHEN ?='cancelled' THEN 'cancelled' ELSE cancellation_state END, updated_at=? WHERE session_request_id=?",
                (terminal_state, terminal_state, current, session_request_id),
            )
            conn.execute(
                "UPDATE fleet_placement_generations SET state=? WHERE session_request_id=? AND placement_generation=?",
                (terminal_state, session_request_id, placement_generation),
            )
            updated = conn.execute("SELECT * FROM fleet_placements WHERE session_request_id=?", (session_request_id,)).fetchone()
            conn.commit()
            return self._record(updated)
        except FleetPlacementError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetPlacementError("terminal placement transaction failed") from exc
        finally:
            conn.close()

    def record_definitive_fence(
        self,
        session_request_id: str,
        *,
        placement_generation: int,
        method: str,
        evidence_digest: str,
        fence_operation_id: Optional[str] = None,
    ) -> str:
        fence_id = fence_operation_id or ("FENCE-" + uuid.uuid4().hex)
        if not _OPERATION_RE.fullmatch(fence_id) or not fence_id.startswith("FENCE-"):
            raise FleetPlacementError("invalid fence operation id")
        _clean(method, "fence method", max_length=128)
        if method not in _FENCE_METHODS:
            raise FleetPlacementError("fence method does not establish a non-executable prior generation")
        _sha256(evidence_digest, "fence evidence digest")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM fleet_placement_generations WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, placement_generation),
            ).fetchone()
            if row is None:
                raise FleetPlacementError("placement generation is unknown")
            if row["state"] == "retired":
                raise FleetPlacementError("retired generation cannot be fenced again")
            if row["fence_operation_id"] is not None:
                if (
                    row["fence_operation_id"] != fence_id
                    or row["fence_method"] != method
                    or row["fence_evidence_digest"] != evidence_digest
                ):
                    raise PlacementConflict("placement generation already has different fencing evidence")
                conn.commit()
                return fence_id
            conn.execute(
                """
                UPDATE fleet_placement_generations
                   SET state='fenced', fence_operation_id=?, fence_method=?, fence_evidence_digest=?
                 WHERE session_request_id=? AND placement_generation=?
                """,
                (fence_id, method, evidence_digest, session_request_id, placement_generation),
            )
            conn.commit()
            return fence_id
        except FleetPlacementError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetPlacementError("fencing transaction failed") from exc
        finally:
            conn.close()

    def transfer_ownership(
        self,
        session_request_id: str,
        *,
        new_owner_node_id: str,
        now: Optional[float] = None,
    ) -> PlacementRecord:
        current = _finite_time(time.time() if now is None else now, "ownership transfer time")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._active_node(conn, new_owner_node_id)
            row = conn.execute("SELECT * FROM fleet_placements WHERE session_request_id=?", (session_request_id,)).fetchone()
            if row is None:
                raise FleetPlacementError("placement is unknown")
            if row["cancellation_state"] != "none":
                raise FleetPlacementError("cancelled/pending-cancel placement cannot transfer ownership")
            generation = conn.execute(
                "SELECT * FROM fleet_placement_generations WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, row["placement_generation"]),
            ).fetchone()
            if generation is None:
                raise FleetPlacementError("current placement generation is missing")
            if generation["state"] not in _TERMINAL_STATES | {"fenced"}:
                raise FleetPlacementError("ownership transfer requires definitive prior-owner terminal/fencing state")
            next_generation = int(row["placement_generation"]) + 1
            dispatch_id = "DISPATCH-" + uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO fleet_placement_generations(
                    session_request_id, placement_generation, owner_node_id, state
                ) VALUES(?, ?, ?, 'active')
                """,
                (session_request_id, next_generation, new_owner_node_id),
            )
            conn.execute(
                """
                UPDATE fleet_placements
                   SET owner_node_id=?, placement_generation=?, state='dispatching',
                       dispatch_operation_id=?, updated_at=?
                 WHERE session_request_id=?
                """,
                (new_owner_node_id, next_generation, dispatch_id, current, session_request_id),
            )
            updated = conn.execute("SELECT * FROM fleet_placements WHERE session_request_id=?", (session_request_id,)).fetchone()
            conn.commit()
            return self._record(updated)
        except FleetPlacementError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetPlacementError("ownership transfer transaction failed") from exc
        finally:
            conn.close()

    def retire_generation(
        self,
        session_request_id: str,
        *,
        placement_generation: int,
        signer: ControlCenterKeyPair,
        now: Optional[float] = None,
    ) -> GenerationRetirementAuthorization:
        current = _finite_time(time.time() if now is None else now, "generation retirement time")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM fleet_placement_generations WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, placement_generation),
            ).fetchone()
            if row is None:
                raise FleetPlacementError("placement generation is unknown")
            if row["state"] == "active":
                raise FleetPlacementError("active generation cannot be retired")
            if row["state"] != "retired":
                conn.execute(
                    "UPDATE fleet_placement_generations SET state='retired', retired_at=? WHERE session_request_id=? AND placement_generation=?",
                    (current, session_request_id, placement_generation),
                )
            conn.commit()
            return GenerationRetirementAuthorization.create(
                session_request_id=session_request_id,
                placement_generation=placement_generation,
                owner_node_id=row["owner_node_id"],
                control_center_id=self.control_center_id,
                signer=signer,
            )
        except FleetPlacementError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetPlacementError("generation retirement transaction failed") from exc
        finally:
            conn.close()


@dataclass(frozen=True)
class NodeAllocationState:
    session_request_id: str
    run_id: Optional[str]
    request_digest: Optional[str]
    placement_generation: int
    state: str
    requested_slots: int
    retired: bool = False


class NodeAdmissionStore:
    """Durable Node-side dedupe, cancellation tombstones, and capacity authority."""

    def __init__(
        self,
        path: Path | str,
        *,
        node_id: str,
        control_center_id: str,
        control_center_public_key_b64: str,
        max_sessions: int,
    ) -> None:
        if not _NODE_RE.fullmatch(node_id):
            raise FleetPlacementError("invalid Node admission identity")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.node_id = node_id
        self.control_center_id = _clean(control_center_id, "control center id")
        self.control_center_public_key_b64 = control_center_public_key_b64
        public_key_fingerprint(control_center_public_key_b64)
        self.max_sessions = _positive_int(max_sessions, "Node max sessions")
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
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS node_admission_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS node_reservations (
                    session_request_id TEXT NOT NULL,
                    placement_generation INTEGER NOT NULL,
                    run_id TEXT NOT NULL,
                    request_digest TEXT NOT NULL,
                    requested_slots INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    dispatch_operation_id TEXT NOT NULL,
                    PRIMARY KEY(session_request_id, placement_generation)
                );
                CREATE TABLE IF NOT EXISTS node_cancellations (
                    session_request_id TEXT NOT NULL,
                    placement_generation INTEGER NOT NULL,
                    cancellation_operation_id TEXT NOT NULL,
                    PRIMARY KEY(session_request_id, placement_generation)
                );
                CREATE TABLE IF NOT EXISTS node_terminal_tombstones (
                    session_request_id TEXT NOT NULL,
                    placement_generation INTEGER NOT NULL,
                    run_id TEXT,
                    request_digest TEXT,
                    terminal_state TEXT NOT NULL,
                    retired INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(session_request_id, placement_generation)
                );
                CREATE TABLE IF NOT EXISTS node_retired_generations (
                    session_request_id TEXT NOT NULL,
                    placement_generation INTEGER NOT NULL,
                    PRIMARY KEY(session_request_id, placement_generation)
                );
                """
            )
            expected = {
                "node_id": self.node_id,
                "control_center_id": self.control_center_id,
                "control_center_key_fingerprint": public_key_fingerprint(self.control_center_public_key_b64),
                "max_sessions": str(self.max_sessions),
            }
            for key, value in expected.items():
                row = conn.execute("SELECT value FROM node_admission_meta WHERE key=?", (key,)).fetchone()
                if row is None:
                    conn.execute("INSERT INTO node_admission_meta(key, value) VALUES(?, ?)", (key, value))
                elif row["value"] != value:
                    raise FleetPlacementError(f"Node admission database {key} does not match configuration")
        except sqlite3.Error as exc:
            raise FleetPlacementError("cannot initialize Node admission store") from exc
        finally:
            conn.close()

    @staticmethod
    def _state(row: sqlite3.Row, *, retired: bool = False) -> NodeAllocationState:
        return NodeAllocationState(
            session_request_id=row["session_request_id"],
            run_id=row["run_id"] if "run_id" in row.keys() else None,
            request_digest=row["request_digest"] if "request_digest" in row.keys() else None,
            placement_generation=int(row["placement_generation"]),
            state=row["state"] if "state" in row.keys() else row["terminal_state"],
            requested_slots=int(row["requested_slots"]) if "requested_slots" in row.keys() else 0,
            retired=retired,
        )

    def _verify_authorization(self, request: SessionRequest, authorization: PlacementAuthorization) -> None:
        authorization.verify(self.control_center_public_key_b64, control_center_id=self.control_center_id)
        expected = (
            authorization.session_request_id == request.session_request_id
            and authorization.run_id == str(request.run_id)
            and hmac.compare_digest(authorization.request_digest, request.request_digest)
            and authorization.owner_node_id == self.node_id
        )
        if not expected:
            raise PlacementConflict("placement authorization does not bind this exact Node/session request")

    def admit(self, request: SessionRequest, authorization: PlacementAuthorization) -> NodeAllocationState:
        self._verify_authorization(request, authorization)
        generation = _positive_int(authorization.placement_generation, "placement generation")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            retired = conn.execute(
                "SELECT 1 FROM node_retired_generations WHERE session_request_id=? AND placement_generation=?",
                (request.session_request_id, generation),
            ).fetchone()
            if retired is not None:
                raise FleetPlacementError("retired placement generation can never authorize execution")
            cancellation = conn.execute(
                "SELECT * FROM node_cancellations WHERE session_request_id=? AND placement_generation=?",
                (request.session_request_id, generation),
            ).fetchone()
            if cancellation is not None:
                tombstone = conn.execute(
                    "SELECT * FROM node_terminal_tombstones WHERE session_request_id=? AND placement_generation=?",
                    (request.session_request_id, generation),
                ).fetchone()
                if tombstone is None:
                    conn.execute(
                        "INSERT INTO node_terminal_tombstones(session_request_id, placement_generation, run_id, request_digest, terminal_state) VALUES(?, ?, ?, ?, 'cancelled')",
                        (request.session_request_id, generation, str(request.run_id), request.request_digest),
                    )
                else:
                    if tombstone["run_id"] is not None and tombstone["run_id"] != str(request.run_id):
                        raise PlacementConflict("cancel-before-allocation tombstone conflicts with run id")
                    if tombstone["request_digest"] is not None and tombstone["request_digest"] != request.request_digest:
                        raise PlacementConflict("cancel-before-allocation tombstone conflicts with request digest")
                    conn.execute(
                        "UPDATE node_terminal_tombstones SET run_id=COALESCE(run_id, ?), request_digest=COALESCE(request_digest, ?) WHERE session_request_id=? AND placement_generation=?",
                        (str(request.run_id), request.request_digest, request.session_request_id, generation),
                    )
                row = conn.execute(
                    "SELECT * FROM node_terminal_tombstones WHERE session_request_id=? AND placement_generation=?",
                    (request.session_request_id, generation),
                ).fetchone()
                conn.commit()
                return self._state(row, retired=bool(row["retired"]))

            tombstone = conn.execute(
                "SELECT * FROM node_terminal_tombstones WHERE session_request_id=? AND placement_generation=?",
                (request.session_request_id, generation),
            ).fetchone()
            if tombstone is not None:
                if tombstone["run_id"] != str(request.run_id) or tombstone["request_digest"] != request.request_digest:
                    raise PlacementConflict("terminal allocation tombstone conflicts with retried request")
                conn.commit()
                return self._state(tombstone, retired=bool(tombstone["retired"]))

            existing = conn.execute(
                "SELECT * FROM node_reservations WHERE session_request_id=? AND placement_generation=?",
                (request.session_request_id, generation),
            ).fetchone()
            if existing is not None:
                if (
                    existing["run_id"] != str(request.run_id)
                    or existing["request_digest"] != request.request_digest
                    or existing["dispatch_operation_id"] != authorization.dispatch_operation_id
                ):
                    raise PlacementConflict("existing Node reservation conflicts with retried allocation")
                conn.commit()
                return self._state(existing)

            other = conn.execute(
                """
                SELECT run_id, request_digest
                  FROM node_reservations
                 WHERE session_request_id=?
                UNION ALL
                SELECT run_id, request_digest
                  FROM node_terminal_tombstones
                 WHERE session_request_id=? AND run_id IS NOT NULL
                LIMIT 1
                """,
                (request.session_request_id, request.session_request_id),
            ).fetchone()
            if other is not None and (
                other["run_id"] != str(request.run_id)
                or other["request_digest"] != request.request_digest
            ):
                raise PlacementConflict("logical session identity changed across placement generations")

            used = conn.execute(
                "SELECT COALESCE(SUM(requested_slots), 0) AS slots FROM node_reservations WHERE state IN ('reserved','allocated','starting','running','cancellation_pending')"
            ).fetchone()["slots"]
            if int(used) + request.requested_slots > self.max_sessions:
                raise CapacityUnavailable("Node capacity is unavailable for atomic reservation")
            conn.execute(
                """
                INSERT INTO node_reservations(
                    session_request_id, placement_generation, run_id,
                    request_digest, requested_slots, state, dispatch_operation_id
                ) VALUES(?, ?, ?, ?, ?, 'reserved', ?)
                """,
                (
                    request.session_request_id,
                    generation,
                    str(request.run_id),
                    request.request_digest,
                    request.requested_slots,
                    authorization.dispatch_operation_id,
                ),
            )
            row = conn.execute(
                "SELECT * FROM node_reservations WHERE session_request_id=? AND placement_generation=?",
                (request.session_request_id, generation),
            ).fetchone()
            conn.commit()
            return self._state(row)
        except FleetPlacementError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetPlacementError("Node admission transaction failed") from exc
        finally:
            conn.close()

    def transition(
        self,
        session_request_id: str,
        *,
        placement_generation: int,
        new_state: str,
    ) -> NodeAllocationState:
        allowed = {
            "reserved": {"allocated", "cancellation_pending"},
            "allocated": {"starting", "cancellation_pending"},
            "starting": {"running", "cancellation_pending"},
            "running": {"cancellation_pending"},
        }
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM node_reservations WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, placement_generation),
            ).fetchone()
            if row is None:
                tombstone = conn.execute(
                    "SELECT * FROM node_terminal_tombstones WHERE session_request_id=? AND placement_generation=?",
                    (session_request_id, placement_generation),
                ).fetchone()
                if tombstone is not None:
                    conn.commit()
                    return self._state(tombstone, retired=bool(tombstone["retired"]))
                raise FleetPlacementError("Node reservation is unknown")
            if new_state not in allowed.get(row["state"], set()):
                if new_state == row["state"]:
                    conn.commit()
                    return self._state(row)
                raise FleetPlacementError(f"invalid Node allocation transition {row['state']} -> {new_state}")
            if new_state in {"starting", "running"}:
                cancellation = conn.execute(
                    "SELECT 1 FROM node_cancellations WHERE session_request_id=? AND placement_generation=?",
                    (session_request_id, placement_generation),
                ).fetchone()
                if cancellation is not None:
                    raise FleetPlacementError("cancelled placement cannot start/run")
            conn.execute(
                "UPDATE node_reservations SET state=? WHERE session_request_id=? AND placement_generation=?",
                (new_state, session_request_id, placement_generation),
            )
            updated = conn.execute(
                "SELECT * FROM node_reservations WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, placement_generation),
            ).fetchone()
            conn.commit()
            return self._state(updated)
        except FleetPlacementError:
            conn.rollback()
            raise
        finally:
            conn.close()

    def cancel(
        self,
        session_request_id: str,
        *,
        placement_generation: int,
        cancellation_operation_id: str,
    ) -> NodeAllocationState:
        if not _OPERATION_RE.fullmatch(cancellation_operation_id) or not cancellation_operation_id.startswith("CANCEL-"):
            raise FleetPlacementError("invalid cancellation operation id")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing_cancel = conn.execute(
                "SELECT cancellation_operation_id FROM node_cancellations WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, placement_generation),
            ).fetchone()
            if existing_cancel is not None and existing_cancel["cancellation_operation_id"] != cancellation_operation_id:
                raise PlacementConflict("Node already recorded a different cancellation operation")
            if existing_cancel is None:
                conn.execute(
                    "INSERT INTO node_cancellations(session_request_id, placement_generation, cancellation_operation_id) VALUES(?, ?, ?)",
                    (session_request_id, placement_generation, cancellation_operation_id),
                )
            reservation = conn.execute(
                "SELECT * FROM node_reservations WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, placement_generation),
            ).fetchone()
            if reservation is None:
                tombstone = conn.execute(
                    "SELECT * FROM node_terminal_tombstones WHERE session_request_id=? AND placement_generation=?",
                    (session_request_id, placement_generation),
                ).fetchone()
                if tombstone is None:
                    conn.execute(
                        "INSERT INTO node_terminal_tombstones(session_request_id, placement_generation, run_id, request_digest, terminal_state) VALUES(?, ?, NULL, NULL, 'cancelled')",
                        (session_request_id, placement_generation),
                    )
                    tombstone = conn.execute(
                        "SELECT * FROM node_terminal_tombstones WHERE session_request_id=? AND placement_generation=?",
                        (session_request_id, placement_generation),
                    ).fetchone()
                conn.commit()
                return self._state(tombstone, retired=bool(tombstone["retired"]))
            if reservation["state"] != "cancellation_pending":
                conn.execute(
                    "UPDATE node_reservations SET state='cancellation_pending' WHERE session_request_id=? AND placement_generation=?",
                    (session_request_id, placement_generation),
                )
            updated = conn.execute(
                "SELECT * FROM node_reservations WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, placement_generation),
            ).fetchone()
            conn.commit()
            return self._state(updated)
        except FleetPlacementError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetPlacementError("Node cancellation transaction failed") from exc
        finally:
            conn.close()

    def mark_terminal(
        self,
        session_request_id: str,
        *,
        placement_generation: int,
        terminal_state: str,
    ) -> NodeAllocationState:
        if terminal_state not in _TERMINAL_STATES:
            raise FleetPlacementError("invalid Node terminal state")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM node_terminal_tombstones WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, placement_generation),
            ).fetchone()
            reservation = conn.execute(
                "SELECT * FROM node_reservations WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, placement_generation),
            ).fetchone()
            if existing is not None:
                if existing["terminal_state"] != terminal_state:
                    raise PlacementConflict("terminal tombstone already records another state")
                conn.commit()
                return self._state(existing, retired=bool(existing["retired"]))
            if reservation is None:
                raise FleetPlacementError("cannot terminalize unknown Node allocation")
            if reservation["state"] == "cancellation_pending" and terminal_state != "cancelled":
                raise FleetPlacementError("cancellation-pending allocation must resolve as cancelled")
            conn.execute(
                """
                INSERT INTO node_terminal_tombstones(
                    session_request_id, placement_generation, run_id,
                    request_digest, terminal_state
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    session_request_id,
                    placement_generation,
                    reservation["run_id"],
                    reservation["request_digest"],
                    terminal_state,
                ),
            )
            conn.execute(
                "DELETE FROM node_reservations WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, placement_generation),
            )
            row = conn.execute(
                "SELECT * FROM node_terminal_tombstones WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, placement_generation),
            ).fetchone()
            conn.commit()
            return self._state(row, retired=False)
        except FleetPlacementError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetPlacementError("Node terminal transaction failed") from exc
        finally:
            conn.close()

    def retire_generation(self, authorization: GenerationRetirementAuthorization) -> NodeAllocationState:
        authorization.verify(self.control_center_public_key_b64, control_center_id=self.control_center_id)
        if authorization.owner_node_id != self.node_id:
            raise FleetPlacementError("generation retirement is addressed to another Node")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                "SELECT 1 FROM node_reservations WHERE session_request_id=? AND placement_generation=?",
                (authorization.session_request_id, authorization.placement_generation),
            ).fetchone()
            if active is not None:
                raise FleetPlacementError("active Node reservation cannot be retired")
            row = conn.execute(
                "SELECT * FROM node_terminal_tombstones WHERE session_request_id=? AND placement_generation=?",
                (authorization.session_request_id, authorization.placement_generation),
            ).fetchone()
            if row is None:
                raise FleetPlacementError("Node terminal tombstone is missing")
            conn.execute(
                "INSERT OR IGNORE INTO node_retired_generations(session_request_id, placement_generation) VALUES(?, ?)",
                (authorization.session_request_id, authorization.placement_generation),
            )
            if not bool(row["retired"]):
                conn.execute(
                    "UPDATE node_terminal_tombstones SET retired=1 WHERE session_request_id=? AND placement_generation=?",
                    (authorization.session_request_id, authorization.placement_generation),
                )
                row = conn.execute(
                    "SELECT * FROM node_terminal_tombstones WHERE session_request_id=? AND placement_generation=?",
                    (authorization.session_request_id, authorization.placement_generation),
                ).fetchone()
            conn.commit()
            return self._state(row, retired=True)
        except FleetPlacementError:
            conn.rollback()
            raise
        finally:
            conn.close()

    def garbage_collect_tombstone(self, session_request_id: str, *, placement_generation: int) -> None:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT retired FROM node_terminal_tombstones WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, placement_generation),
            ).fetchone()
            if row is None:
                conn.commit()
                return
            if not bool(row["retired"]):
                raise FleetPlacementError("terminal tombstone cannot be deleted before generation retirement")
            conn.execute(
                "DELETE FROM node_terminal_tombstones WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, placement_generation),
            )
            conn.execute(
                "DELETE FROM node_cancellations WHERE session_request_id=? AND placement_generation=?",
                (session_request_id, placement_generation),
            )
            conn.commit()
        except FleetPlacementError:
            conn.rollback()
            raise
        finally:
            conn.close()
