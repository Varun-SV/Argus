"""Authenticated Fleet heartbeat API with durable Node boot ordering.

The bulk of the v1 heartbeat implementation remains in ``heartbeat_impl``. This
module keeps the public import surface stable while hardening the Control Center's
latest-heartbeat pointer against delayed heartbeats from a retired Node boot.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from .heartbeat_impl import *  # noqa: F401,F403
from . import heartbeat_impl as _impl


@dataclass(frozen=True)
class NodeHeartbeat(_impl.NodeHeartbeat):
    """Signed heartbeat carrying explicit proof of a Node boot transition."""

    previous_boot_id: Optional[str] = None

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
        previous_boot_id: Optional[str] = None,
    ) -> "NodeHeartbeat":
        heartbeat = cls(
            heartbeat_id=heartbeat_id or ("HEARTBEAT-" + _impl.uuid.uuid4().hex),
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
            node_time=float(_impl.time.time() if node_time is None else node_time),
            monotonic_seconds=float(
                _impl.time.monotonic()
                if monotonic_seconds is None
                else monotonic_seconds
            ),
            public_key_b64=key_pair.public_key_b64,
            proof_signature="",
            previous_boot_id=previous_boot_id,
        )
        heartbeat._validate_unsigned()
        signature = key_pair.sign(
            _impl.canonical_signed_message("node-heartbeat", heartbeat.payload())
        )
        return cls(**{**heartbeat.__dict__, "proof_signature": signature})

    def _validate_unsigned(self) -> None:
        super()._validate_unsigned()
        if self.previous_boot_id is not None:
            if not _impl._BOOT_ID_RE.fullmatch(self.previous_boot_id):
                raise FleetHeartbeatError("invalid previous Node boot id")
            if self.previous_boot_id == self.boot_id:
                raise FleetHeartbeatError("previous Node boot id must differ from current boot id")

    def payload(self) -> dict[str, object]:
        payload = super().payload()
        payload["previous_boot_id"] = self.previous_boot_id
        return payload


class FleetHeartbeatRegistry(_impl.FleetHeartbeatRegistry):
    """Heartbeat registry that durably prevents retired boots becoming latest."""

    def _initialize(self) -> None:
        super()._initialize()
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fleet_node_current_boot (
                    node_id TEXT PRIMARY KEY REFERENCES nodes(node_id),
                    boot_id TEXT NOT NULL
                )
                """
            )
        except _impl.sqlite3.Error as exc:
            raise FleetHeartbeatError(
                "cannot initialize Fleet heartbeat boot state"
            ) from exc
        finally:
            conn.close()

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
        received = _impl._finite_nonnegative(
            _impl.time.time() if received_at is None else received_at,
            "Control Center heartbeat receipt time",
        )
        payload_json = _impl._canonical(heartbeat.payload()).decode("utf-8")
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
            if not _impl.hmac.compare_digest(
                node["public_key_fingerprint"],
                _impl.public_key_fingerprint(heartbeat.public_key_b64),
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
            if head is not None and heartbeat.sequence <= int(head["last_sequence"]):
                raise FleetHeartbeatError("stale or replayed heartbeat sequence")

            current_boot = conn.execute(
                "SELECT boot_id FROM fleet_node_current_boot WHERE node_id=?",
                (heartbeat.node_id,),
            ).fetchone()
            if current_boot is None:
                if heartbeat.previous_boot_id is not None:
                    raise FleetHeartbeatError(
                        "initial Node boot cannot claim an unobserved predecessor"
                    )
                conn.execute(
                    "INSERT INTO fleet_node_current_boot(node_id, boot_id) VALUES(?, ?)",
                    (heartbeat.node_id, heartbeat.boot_id),
                )
            elif current_boot["boot_id"] != heartbeat.boot_id:
                # A different boot may become current only when its signed heartbeat
                # explicitly names the currently authoritative predecessor. This
                # makes an unseen delayed old boot distinguishable from a real
                # restart without trusting packet arrival order or Node wall time.
                if heartbeat.previous_boot_id != current_boot["boot_id"]:
                    raise FleetHeartbeatError(
                        "heartbeat does not prove transition from current Node boot"
                    )
                if head is not None:
                    raise FleetHeartbeatError("heartbeat belongs to a retired Node boot")
                conn.execute(
                    "UPDATE fleet_node_current_boot SET boot_id=? WHERE node_id=?",
                    (heartbeat.boot_id, heartbeat.node_id),
                )
            # Once this boot is authoritative, retaining its signed predecessor proof
            # is harmless and must remain idempotent: the proof cannot move authority
            # because the heartbeat boot already equals fleet_node_current_boot.

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
        except _impl.sqlite3.Error as exc:
            conn.rollback()
            raise FleetHeartbeatError("heartbeat registry transaction failed") from exc
        finally:
            conn.close()
