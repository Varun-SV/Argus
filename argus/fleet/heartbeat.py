"""Authenticated Fleet heartbeat API with durable Node boot ordering.

The bulk of the v1 heartbeat implementation remains in ``heartbeat_impl``.  This
module keeps the public import surface stable while hardening the Control Center's
latest-heartbeat pointer against delayed heartbeats from a retired Node boot.
"""
from __future__ import annotations

from typing import Optional

from .heartbeat_impl import *  # noqa: F401,F403
from . import heartbeat_impl as _impl


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
            if (
                head is not None
                and heartbeat.sequence <= int(head["last_sequence"])
            ):
                raise FleetHeartbeatError(
                    "stale or replayed heartbeat sequence"
                )

            current_boot = conn.execute(
                "SELECT boot_id FROM fleet_node_current_boot WHERE node_id=?",
                (heartbeat.node_id,),
            ).fetchone()
            if current_boot is None:
                conn.execute(
                    "INSERT INTO fleet_node_current_boot(node_id, boot_id) VALUES(?, ?)",
                    (heartbeat.node_id, heartbeat.boot_id),
                )
            elif current_boot["boot_id"] != heartbeat.boot_id:
                # A boot that already has accepted history is retired once another
                # boot becomes current.  It may still be replayed idempotently by
                # heartbeat_id above, but it can never reclaim the latest pointer.
                retired = conn.execute(
                    """
                    SELECT 1
                      FROM fleet_heartbeat_heads
                     WHERE node_id=? AND boot_id=?
                    """,
                    (heartbeat.node_id, heartbeat.boot_id),
                ).fetchone()
                if retired is not None:
                    raise FleetHeartbeatError(
                        "heartbeat belongs to a retired Node boot"
                    )
                conn.execute(
                    "UPDATE fleet_node_current_boot SET boot_id=? WHERE node_id=?",
                    (heartbeat.boot_id, heartbeat.node_id),
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
        except _impl.sqlite3.Error as exc:
            conn.rollback()
            raise FleetHeartbeatError(
                "heartbeat registry transaction failed"
            ) from exc
        finally:
            conn.close()
