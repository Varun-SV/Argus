"""Authenticated Fleet heartbeats, immutable capabilities, and clock assessment.

Heartbeat state is advisory scheduling data. It never acts as a fencing proof.
Every accepted heartbeat is signed by an active enrolled Node identity and the
Control Center records its own receipt timestamp separately from Node time.
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
from typing import Optional, Sequence

from .identity import (
    FleetIdentityError,
    NodeKeyPair,
    canonical_signed_message,
    public_key_fingerprint,
    verify_signature,
)

FLEET_HEARTBEAT_VERSION = "argus-fleet-heartbeat-v1"
CLOCK_ASSESSMENT_VERSION = "argus-fleet-clock-assessment-v1"
_NODE_ID_RE = re.compile(r"^NODE-[0-9a-f]{32}$")
_HEARTBEAT_ID_RE = re.compile(r"^HEARTBEAT-[0-9a-f]{32}$")
_BOOT_ID_RE = re.compile(r"^BOOTID-[0-9a-f]{32}$")
_PROBE_ID_RE = re.compile(r"^CLOCKPROBE-[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SYNC_STATES = frozenset({"healthy", "unknown", "degraded", "unsynchronized"})


class FleetHeartbeatError(RuntimeError):
    """Heartbeat/capability state cannot be trusted or persisted safely."""


class HeartbeatConflict(FleetHeartbeatError):
    """A stable heartbeat/probe identity was reused with conflicting data."""


def _clean_text(value: str, label: str, *, max_length: int = 255) -> str:
    if not isinstance(value, str):
        raise FleetHeartbeatError(f"{label} must be a string")
    cleaned = value.strip()
    if not cleaned or cleaned != value or len(cleaned) > max_length:
        raise FleetHeartbeatError(f"{label} must be canonical and non-empty")
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
        raise FleetHeartbeatError("Fleet heartbeat value is not canonical JSON") from exc


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _finite_nonnegative(value: float, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise FleetHeartbeatError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise FleetHeartbeatError(f"{label} must be finite and non-negative")
    return result


@dataclass(frozen=True)
class ImageAdvertisement:
    alias: str
    image_id: str
    digest: str
    guest_os: str

    def __post_init__(self) -> None:
        _clean_text(self.alias, "image alias")
        _clean_text(self.image_id, "image id")
        _clean_text(self.guest_os, "image guest os", max_length=64)
        if not isinstance(self.digest, str) or not _SHA256_RE.fullmatch(self.digest):
            raise FleetHeartbeatError(
                "image digest must be a lowercase sha256:<64-hex> immutable identity"
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "alias": self.alias,
            "image_id": self.image_id,
            "digest": self.digest,
            "guest_os": self.guest_os,
        }


@dataclass(frozen=True)
class CapacityAdvertisement:
    max_sessions: int
    active_sessions: int
    memory_mb: int
    available_memory_mb: int
    disk_free_mb: int

    def __post_init__(self) -> None:
        for name in (
            "max_sessions",
            "active_sessions",
            "memory_mb",
            "available_memory_mb",
            "disk_free_mb",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise FleetHeartbeatError(
                    f"capacity {name} must be a non-negative integer"
                )
        if self.max_sessions < 1:
            raise FleetHeartbeatError("capacity max_sessions must be at least one")
        if self.active_sessions > self.max_sessions:
            raise FleetHeartbeatError("active_sessions cannot exceed max_sessions")
        if self.available_memory_mb > self.memory_mb:
            raise FleetHeartbeatError("available_memory_mb cannot exceed memory_mb")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_sessions": self.max_sessions,
            "active_sessions": self.active_sessions,
            "memory_mb": self.memory_mb,
            "available_memory_mb": self.available_memory_mb,
            "disk_free_mb": self.disk_free_mb,
        }


@dataclass(frozen=True)
class NodeHeartbeat:
    heartbeat_id: str
    node_id: str
    boot_id: str
    sequence: int
    control_center_id: str
    agent_version: str
    host_os: str
    provider: str
    capacity: CapacityAdvertisement
    images: tuple[ImageAdvertisement, ...]
    active_session_ids: tuple[str, ...]
    degraded_conditions: tuple[str, ...]
    sync_status: str
    node_time: float
    monotonic_seconds: float
    public_key_b64: str
    proof_signature: str

    @classmethod
    def create(
        cls,
        *,
        node_id: str,
        control_center_id: str,
        key_pair: NodeKeyPair,
        boot_id: str,
        sequence: int,
        agent_version: str,
        host_os: str,
        provider: str,
        capacity: CapacityAdvertisement,
        images: Sequence[ImageAdvertisement],
        active_session_ids: Sequence[str] = (),
        degraded_conditions: Sequence[str] = (),
        sync_status: str = "unknown",
        node_time: Optional[float] = None,
        monotonic_seconds: Optional[float] = None,
        heartbeat_id: Optional[str] = None,
    ) -> "NodeHeartbeat":
        heartbeat = cls(
            heartbeat_id=heartbeat_id or ("HEARTBEAT-" + uuid.uuid4().hex),
            node_id=node_id,
            boot_id=boot_id,
            sequence=sequence,
            control_center_id=control_center_id,
            agent_version=agent_version,
            host_os=host_os,
            provider=provider,
            capacity=capacity,
            images=tuple(images),
            active_session_ids=tuple(active_session_ids),
            degraded_conditions=tuple(degraded_conditions),
            sync_status=sync_status,
            node_time=float(time.time() if node_time is None else node_time),
            monotonic_seconds=float(
                time.monotonic()
                if monotonic_seconds is None
                else monotonic_seconds
            ),
            public_key_b64=key_pair.public_key_b64,
            proof_signature="",
        )
        heartbeat._validate_unsigned()
        signature = key_pair.sign(
            canonical_signed_message("node-heartbeat", heartbeat.payload())
        )
        return cls(**{**heartbeat.__dict__, "proof_signature": signature})

    def _validate_unsigned(self) -> None:
        if not _HEARTBEAT_ID_RE.fullmatch(self.heartbeat_id):
            raise FleetHeartbeatError("invalid heartbeat id")
        if not _NODE_ID_RE.fullmatch(self.node_id):
            raise FleetHeartbeatError("invalid node id")
        if not _BOOT_ID_RE.fullmatch(self.boot_id):
            raise FleetHeartbeatError("invalid Node boot id")
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence < 1
        ):
            raise FleetHeartbeatError("heartbeat sequence must be a positive integer")
        _clean_text(self.control_center_id, "control center id")
        _clean_text(self.agent_version, "agent version", max_length=128)
        _clean_text(self.host_os, "host os", max_length=64)
        _clean_text(self.provider, "provider", max_length=64)
        _finite_nonnegative(self.node_time, "Node wall time")
        _finite_nonnegative(self.monotonic_seconds, "Node monotonic time")
        if self.sync_status not in _SYNC_STATES:
            raise FleetHeartbeatError("unsupported Node clock sync status")
        if len(self.images) > 4096:
            raise FleetHeartbeatError("heartbeat image inventory is unreasonably large")
        aliases: set[str] = set()
        for image in self.images:
            if not isinstance(image, ImageAdvertisement):
                raise FleetHeartbeatError("images must be ImageAdvertisement values")
            if image.alias in aliases:
                raise FleetHeartbeatError(
                    "image aliases must be unique within a heartbeat"
                )
            aliases.add(image.alias)
        for value in self.active_session_ids:
            _clean_text(value, "active session id", max_length=128)
        if len(set(self.active_session_ids)) != len(self.active_session_ids):
            raise FleetHeartbeatError("active session ids must be unique")
        for value in self.degraded_conditions:
            _clean_text(value, "degraded condition", max_length=256)
        if len(set(self.degraded_conditions)) != len(self.degraded_conditions):
            raise FleetHeartbeatError("degraded conditions must be unique")
        public_key_fingerprint(self.public_key_b64)

    def payload(self) -> dict[str, object]:
        return {
            "heartbeat_version": FLEET_HEARTBEAT_VERSION,
            "heartbeat_id": self.heartbeat_id,
            "node_id": self.node_id,
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "control_center_id": self.control_center_id,
            "agent_version": self.agent_version,
            "host_os": self.host_os,
            "provider": self.provider,
            "capacity": self.capacity.to_dict(),
            "images": [image.to_dict() for image in self.images],
            "active_session_ids": list(self.active_session_ids),
            "degraded_conditions": list(self.degraded_conditions),
            "sync_status": self.sync_status,
            "node_time": self.node_time,
            "monotonic_seconds": self.monotonic_seconds,
            "public_key_b64": self.public_key_b64,
        }

    @property
    def request_digest(self) -> str:
        return _digest(self.payload())

    def verify_signature(self) -> None:
        self._validate_unsigned()
        try:
            verify_signature(
                self.public_key_b64,
                canonical_signed_message("node-heartbeat", self.payload()),
                self.proof_signature,
            )
        except FleetIdentityError as exc:
            raise FleetHeartbeatError("Node heartbeat signature is invalid") from exc


@dataclass(frozen=True)
class HeartbeatReceipt:
    heartbeat_id: str
    node_id: str
    received_at: float
    request_digest: str
    health_state: str


@dataclass(frozen=True)
class NodeHealth:
    node_id: str
    state: str
    last_received_at: Optional[float]
    heartbeat_age_seconds: Optional[float]
    degraded_conditions: tuple[str, ...]
    sync_status: Optional[str]


@dataclass(frozen=True)
class ClockProbeChallenge:
    probe_id: str
    node_id: str
    control_center_id: str
    control_sent_at: float


@dataclass(frozen=True)
class ClockProbeResponse:
    probe_id: str
    node_id: str
    control_center_id: str
    boot_id: str
    node_received_at: float
    node_sent_at: float
    node_monotonic_seconds: float
    public_key_b64: str
    proof_signature: str

    @classmethod
    def create(
        cls,
        *,
        challenge: ClockProbeChallenge,
        key_pair: NodeKeyPair,
        boot_id: str,
        node_received_at: float,
        node_sent_at: float,
        node_monotonic_seconds: float,
    ) -> "ClockProbeResponse":
        response = cls(
            probe_id=challenge.probe_id,
            node_id=challenge.node_id,
            control_center_id=challenge.control_center_id,
            boot_id=boot_id,
            node_received_at=float(node_received_at),
            node_sent_at=float(node_sent_at),
            node_monotonic_seconds=float(node_monotonic_seconds),
            public_key_b64=key_pair.public_key_b64,
            proof_signature="",
        )
        response._validate_unsigned()
        signature = key_pair.sign(
            canonical_signed_message("node-clock-probe", response.payload())
        )
        return cls(**{**response.__dict__, "proof_signature": signature})

    def _validate_unsigned(self) -> None:
        if not _PROBE_ID_RE.fullmatch(self.probe_id):
            raise FleetHeartbeatError("invalid clock probe id")
        if not _NODE_ID_RE.fullmatch(self.node_id):
            raise FleetHeartbeatError("invalid node id")
        if not _BOOT_ID_RE.fullmatch(self.boot_id):
            raise FleetHeartbeatError("invalid Node boot id")
        _clean_text(self.control_center_id, "control center id")
        received = _finite_nonnegative(
            self.node_received_at, "Node probe receive time"
        )
        sent = _finite_nonnegative(self.node_sent_at, "Node probe send time")
        _finite_nonnegative(
            self.node_monotonic_seconds, "Node probe monotonic time"
        )
        if sent < received:
            raise FleetHeartbeatError(
                "Node clock probe send time precedes receive time"
            )

    def payload(self) -> dict[str, object]:
        return {
            "clock_assessment_version": CLOCK_ASSESSMENT_VERSION,
            "probe_id": self.probe_id,
            "node_id": self.node_id,
            "control_center_id": self.control_center_id,
            "boot_id": self.boot_id,
            "node_received_at": self.node_received_at,
            "node_sent_at": self.node_sent_at,
            "node_monotonic_seconds": self.node_monotonic_seconds,
            "public_key_b64": self.public_key_b64,
        }

    def verify_signature(self) -> None:
        self._validate_unsigned()
        try:
            verify_signature(
                self.public_key_b64,
                canonical_signed_message("node-clock-probe", self.payload()),
                self.proof_signature,
            )
        except FleetIdentityError as exc:
            raise FleetHeartbeatError("Node clock probe signature is invalid") from exc


@dataclass(frozen=True)
class ClockAssessment:
    probe_id: str
    node_id: str
    measured_at: float
    node_minus_control_offset_ms: float
    uncertainty_ms: float
    round_trip_ms: float
    sample_age_ms: float
    boot_id: str


class FleetHeartbeatRegistry:
    """Control Center heartbeat/capability state over an enrollment registry DB."""

    def __init__(self, path, *, control_center_id: str) -> None:
        self.path = path
        self.control_center_id = _clean_text(
            control_center_id, "control center id"
        )
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
            meta = conn.execute(
                "SELECT value FROM fleet_meta WHERE key='control_center_id'"
            ).fetchone()
            if meta is None or meta["value"] != self.control_center_id:
                raise FleetHeartbeatError(
                    "heartbeat registry requires the matching initialized "
                    "Fleet enrollment registry"
                )
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS fleet_heartbeats (
                    heartbeat_id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL REFERENCES nodes(node_id),
                    boot_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    request_digest TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    received_at REAL NOT NULL,
                    health_state TEXT NOT NULL,
                    UNIQUE(node_id, boot_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS fleet_heartbeat_heads (
                    node_id TEXT NOT NULL REFERENCES nodes(node_id),
                    boot_id TEXT NOT NULL,
                    last_sequence INTEGER NOT NULL,
                    last_heartbeat_id TEXT NOT NULL,
                    PRIMARY KEY(node_id, boot_id)
                );
                CREATE TABLE IF NOT EXISTS fleet_node_latest_heartbeat (
                    node_id TEXT PRIMARY KEY REFERENCES nodes(node_id),
                    heartbeat_id TEXT NOT NULL REFERENCES fleet_heartbeats(heartbeat_id)
                );
                CREATE TABLE IF NOT EXISTS fleet_clock_probes (
                    probe_id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL REFERENCES nodes(node_id),
                    control_sent_at REAL NOT NULL,
                    state TEXT NOT NULL,
                    control_received_at REAL,
                    response_digest TEXT
                );
                CREATE TABLE IF NOT EXISTS fleet_clock_assessments (
                    probe_id TEXT PRIMARY KEY REFERENCES fleet_clock_probes(probe_id),
                    node_id TEXT NOT NULL REFERENCES nodes(node_id),
                    measured_at REAL NOT NULL,
                    boot_id TEXT NOT NULL,
                    node_minus_control_offset_ms REAL NOT NULL,
                    uncertainty_ms REAL NOT NULL,
                    round_trip_ms REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS fleet_node_latest_clock (
                    node_id TEXT PRIMARY KEY REFERENCES nodes(node_id),
                    probe_id TEXT NOT NULL REFERENCES fleet_clock_assessments(probe_id)
                );
                """
            )
        except sqlite3.Error as exc:
            raise FleetHeartbeatError(
                "cannot initialize Fleet heartbeat registry"
            ) from exc
        finally:
            conn.close()

    def _active_node(
        self, conn: sqlite3.Connection, node_id: str
    ) -> sqlite3.Row:
        if not _NODE_ID_RE.fullmatch(node_id):
            raise FleetHeartbeatError("invalid node id")
        row = conn.execute(
            """
            SELECT node_id, state, public_key_b64, public_key_fingerprint
              FROM nodes
             WHERE node_id=?
            """,
            (node_id,),
        ).fetchone()
        if row is None:
            raise FleetHeartbeatError("Node identity is unknown")
        if row["state"] != "active":
            raise FleetHeartbeatError("Node identity is not active")
        return row

    def accept_heartbeat(
        self,
        heartbeat: NodeHeartbeat,
        *,
        received_at: Optional[float] = None,
    ) -> HeartbeatReceipt:
        heartbeat.verify_signature()
        if heartbeat.control_center_id != self.control_center_id:
            raise FleetHeartbeatError(
                "heartbeat targets a different Control Center"
            )
        received = _finite_nonnegative(
            time.time() if received_at is None else received_at,
            "Control Center heartbeat receipt time",
        )
        payload_json = _canonical(heartbeat.payload()).decode("utf-8")
        digest = heartbeat.request_digest
        health_state = (
            "degraded"
            if heartbeat.degraded_conditions
            or heartbeat.sync_status in {"degraded", "unsynchronized"}
            else "healthy"
        )

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            node = self._active_node(conn, heartbeat.node_id)
            if node["public_key_b64"] != heartbeat.public_key_b64:
                raise HeartbeatConflict(
                    "heartbeat key does not match enrolled Node identity"
                )
            if not hmac.compare_digest(
                node["public_key_fingerprint"],
                public_key_fingerprint(heartbeat.public_key_b64),
            ):
                raise HeartbeatConflict(
                    "heartbeat public-key fingerprint does not match enrollment"
                )

            existing = conn.execute(
                "SELECT * FROM fleet_heartbeats WHERE heartbeat_id=?",
                (heartbeat.heartbeat_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["request_digest"] != digest
                    or existing["node_id"] != heartbeat.node_id
                ):
                    raise HeartbeatConflict(
                        "heartbeat id was reused with conflicting immutable content"
                    )
                conn.commit()
                return HeartbeatReceipt(
                    heartbeat_id=existing["heartbeat_id"],
                    node_id=existing["node_id"],
                    received_at=float(existing["received_at"]),
                    request_digest=existing["request_digest"],
                    health_state=existing["health_state"],
                )

            head = conn.execute(
                """
                SELECT last_sequence
                  FROM fleet_heartbeat_heads
                 WHERE node_id=? AND boot_id=?
                """,
                (heartbeat.node_id, heartbeat.boot_id),
            ).fetchone()
            if (
                head is not None
                and heartbeat.sequence <= int(head["last_sequence"])
            ):
                raise FleetHeartbeatError(
                    "stale or replayed heartbeat sequence"
                )

            conn.execute(
                """
                INSERT INTO fleet_heartbeats(
                    heartbeat_id, node_id, boot_id, sequence, request_digest,
                    payload_json, received_at, health_state
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    heartbeat.heartbeat_id,
                    heartbeat.node_id,
                    heartbeat.boot_id,
                    heartbeat.sequence,
                    digest,
                    payload_json,
                    received,
                    health_state,
                ),
            )
            conn.execute(
                """
                INSERT INTO fleet_heartbeat_heads(
                    node_id, boot_id, last_sequence, last_heartbeat_id
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(node_id, boot_id) DO UPDATE SET
                    last_sequence=excluded.last_sequence,
                    last_heartbeat_id=excluded.last_heartbeat_id
                """,
                (
                    heartbeat.node_id,
                    heartbeat.boot_id,
                    heartbeat.sequence,
                    heartbeat.heartbeat_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO fleet_node_latest_heartbeat(node_id, heartbeat_id)
                VALUES(?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    heartbeat_id=excluded.heartbeat_id
                """,
                (heartbeat.node_id, heartbeat.heartbeat_id),
            )
            conn.commit()
            return HeartbeatReceipt(
                heartbeat_id=heartbeat.heartbeat_id,
                node_id=heartbeat.node_id,
                received_at=received,
                request_digest=digest,
                health_state=health_state,
            )
        except FleetHeartbeatError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetHeartbeatError(
                "heartbeat registry transaction failed"
            ) from exc
        finally:
            conn.close()

    def node_health(
        self,
        node_id: str,
        *,
        now: Optional[float] = None,
        disconnected_after_seconds: float = 15.0,
    ) -> NodeHealth:
        threshold = _finite_nonnegative(
            disconnected_after_seconds,
            "heartbeat disconnect threshold",
        )
        current = _finite_nonnegative(
            time.time() if now is None else now,
            "health query time",
        )
        conn = self._connect()
        try:
            self._active_node(conn, node_id)
            row = conn.execute(
                """
                SELECT h.received_at, h.health_state, h.payload_json
                  FROM fleet_node_latest_heartbeat l
                  JOIN fleet_heartbeats h
                    ON h.heartbeat_id=l.heartbeat_id
                 WHERE l.node_id=?
                """,
                (node_id,),
            ).fetchone()
            if row is None:
                return NodeHealth(
                    node_id,
                    "disconnected",
                    None,
                    None,
                    (),
                    None,
                )
            age = max(0.0, current - float(row["received_at"]))
            payload = json.loads(row["payload_json"])
            state = (
                "disconnected"
                if age > threshold
                else row["health_state"]
            )
            return NodeHealth(
                node_id=node_id,
                state=state,
                last_received_at=float(row["received_at"]),
                heartbeat_age_seconds=age,
                degraded_conditions=tuple(
                    payload["degraded_conditions"]
                ),
                sync_status=payload["sync_status"],
            )
        finally:
            conn.close()

    def begin_clock_probe(
        self,
        node_id: str,
        *,
        control_sent_at: Optional[float] = None,
    ) -> ClockProbeChallenge:
        sent = _finite_nonnegative(
            time.time() if control_sent_at is None else control_sent_at,
            "Control Center probe send time",
        )
        probe_id = "CLOCKPROBE-" + uuid.uuid4().hex
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._active_node(conn, node_id)
            conn.execute(
                """
                INSERT INTO fleet_clock_probes(
                    probe_id, node_id, control_sent_at, state
                ) VALUES(?, ?, ?, 'pending')
                """,
                (probe_id, node_id, sent),
            )
            conn.commit()
        except FleetHeartbeatError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetHeartbeatError(
                "cannot persist Fleet clock probe"
            ) from exc
        finally:
            conn.close()
        return ClockProbeChallenge(
            probe_id,
            node_id,
            self.control_center_id,
            sent,
        )

    def complete_clock_probe(
        self,
        response: ClockProbeResponse,
        *,
        control_received_at: Optional[float] = None,
    ) -> ClockAssessment:
        response.verify_signature()
        if response.control_center_id != self.control_center_id:
            raise FleetHeartbeatError(
                "clock probe targets a different Control Center"
            )
        received = _finite_nonnegative(
            (
                time.time()
                if control_received_at is None
                else control_received_at
            ),
            "Control Center probe receive time",
        )
        response_digest = _digest(response.payload())
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            node = self._active_node(conn, response.node_id)
            if node["public_key_b64"] != response.public_key_b64:
                raise HeartbeatConflict(
                    "clock probe key does not match enrolled Node identity"
                )
            probe = conn.execute(
                "SELECT * FROM fleet_clock_probes WHERE probe_id=?",
                (response.probe_id,),
            ).fetchone()
            if probe is None:
                raise FleetHeartbeatError("clock probe is unknown")
            if probe["node_id"] != response.node_id:
                raise HeartbeatConflict(
                    "clock probe response names a different Node"
                )
            if probe["state"] == "completed":
                if probe["response_digest"] != response_digest:
                    raise HeartbeatConflict(
                        "clock probe id was reused with conflicting response"
                    )
                assessment = self._assessment_for_probe(
                    conn,
                    response.probe_id,
                    now=received,
                )
                conn.commit()
                return assessment
            if probe["state"] != "pending":
                raise FleetHeartbeatError("clock probe is not completable")

            t0 = float(probe["control_sent_at"])
            t1 = response.node_received_at
            t2 = response.node_sent_at
            t3 = received
            if t3 < t0:
                raise FleetHeartbeatError(
                    "Control Center probe receive time precedes send time"
                )
            node_processing = t2 - t1
            observed_rtt = t3 - t0
            network_rtt = observed_rtt - node_processing
            if network_rtt < -1e-6:
                raise FleetHeartbeatError(
                    "clock probe timing is inconsistent with the observed "
                    "round trip"
                )
            network_rtt = max(0.0, network_rtt)
            offset_seconds = ((t1 - t0) + (t2 - t3)) / 2.0
            offset_ms = offset_seconds * 1000.0
            uncertainty_ms = (network_rtt * 1000.0) / 2.0
            round_trip_ms = network_rtt * 1000.0

            conn.execute(
                """
                UPDATE fleet_clock_probes
                   SET state='completed',
                       control_received_at=?,
                       response_digest=?
                 WHERE probe_id=?
                """,
                (
                    received,
                    response_digest,
                    response.probe_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO fleet_clock_assessments(
                    probe_id, node_id, measured_at, boot_id,
                    node_minus_control_offset_ms,
                    uncertainty_ms,
                    round_trip_ms
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    response.probe_id,
                    response.node_id,
                    received,
                    response.boot_id,
                    offset_ms,
                    uncertainty_ms,
                    round_trip_ms,
                ),
            )
            conn.execute(
                """
                INSERT INTO fleet_node_latest_clock(node_id, probe_id)
                VALUES(?, ?)
                ON CONFLICT(node_id) DO UPDATE SET
                    probe_id=excluded.probe_id
                """,
                (response.node_id, response.probe_id),
            )
            conn.commit()
            return ClockAssessment(
                probe_id=response.probe_id,
                node_id=response.node_id,
                measured_at=received,
                node_minus_control_offset_ms=offset_ms,
                uncertainty_ms=uncertainty_ms,
                round_trip_ms=round_trip_ms,
                sample_age_ms=0.0,
                boot_id=response.boot_id,
            )
        except FleetHeartbeatError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetHeartbeatError(
                "clock assessment transaction failed"
            ) from exc
        finally:
            conn.close()

    def _assessment_for_probe(
        self,
        conn: sqlite3.Connection,
        probe_id: str,
        *,
        now: float,
    ) -> ClockAssessment:
        row = conn.execute(
            """
            SELECT *
              FROM fleet_clock_assessments
             WHERE probe_id=?
            """,
            (probe_id,),
        ).fetchone()
        if row is None:
            raise FleetHeartbeatError(
                "clock assessment is missing for completed probe"
            )
        return ClockAssessment(
            probe_id=row["probe_id"],
            node_id=row["node_id"],
            measured_at=float(row["measured_at"]),
            node_minus_control_offset_ms=float(
                row["node_minus_control_offset_ms"]
            ),
            uncertainty_ms=float(row["uncertainty_ms"]),
            round_trip_ms=float(row["round_trip_ms"]),
            sample_age_ms=max(
                0.0,
                (now - float(row["measured_at"])) * 1000.0,
            ),
            boot_id=row["boot_id"],
        )

    def latest_clock_assessment(
        self,
        node_id: str,
        *,
        now: Optional[float] = None,
    ) -> Optional[ClockAssessment]:
        current = _finite_nonnegative(
            time.time() if now is None else now,
            "clock query time",
        )
        conn = self._connect()
        try:
            self._active_node(conn, node_id)
            row = conn.execute(
                """
                SELECT probe_id
                  FROM fleet_node_latest_clock
                 WHERE node_id=?
                """,
                (node_id,),
            ).fetchone()
            if row is None:
                return None
            return self._assessment_for_probe(
                conn,
                row["probe_id"],
                now=current,
            )
        finally:
            conn.close()
