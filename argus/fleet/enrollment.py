"""Durable, retry-idempotent Argus Fleet Node enrollment.

The registry implements the trust-boundary semantics in docs/fleet.md:
single-use audience-bound bootstrap credentials, Node proof-of-possession,
stable enrollment request identity, lost-response replay, explicit
acknowledgement, and durable administrative revocation.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .identity import (
    FleetIdentityError,
    NodeKeyPair,
    canonical_signed_message,
    public_key_fingerprint,
    verify_signature,
)

FLEET_REGISTRY_VERSION = "argus-fleet-registry-v1"
BOOTSTRAP_DIGEST_PROFILE = "argus-fleet-bootstrap-sha256-v1"
_ENROLLMENT_ID_RE = re.compile(r"^ENROLL-[0-9a-f]{32}$")
_NODE_ID_RE = re.compile(r"^NODE-[0-9a-f]{32}$")
_BOOTSTRAP_ID_RE = re.compile(r"^BOOT-[0-9a-f]{32}$")


class FleetEnrollmentError(RuntimeError):
    """Enrollment state cannot be accepted or recovered safely."""


class EnrollmentConflict(FleetEnrollmentError):
    """A stable identity was reused with conflicting immutable data."""


@dataclass(frozen=True)
class BootstrapCredential:
    credential_id: str
    secret: str
    control_center_id: str
    expires_at: float


@dataclass(frozen=True)
class EnrollmentRequest:
    enrollment_request_id: str
    control_center_id: str
    public_key_b64: str
    public_key_fingerprint: str
    proof_signature: str

    @classmethod
    def create(
        cls,
        *,
        control_center_id: str,
        key_pair: NodeKeyPair,
        enrollment_request_id: Optional[str] = None,
    ) -> "EnrollmentRequest":
        request_id = enrollment_request_id or ("ENROLL-" + uuid.uuid4().hex)
        if not _ENROLLMENT_ID_RE.fullmatch(request_id):
            raise FleetEnrollmentError("invalid enrollment request id")
        payload = {
            "control_center_id": _clean_control_center_id(control_center_id),
            "enrollment_request_id": request_id,
            "public_key_b64": key_pair.public_key_b64,
            "public_key_fingerprint": key_pair.fingerprint,
        }
        signature = key_pair.sign(canonical_signed_message("node-enrollment", payload))
        return cls(
            enrollment_request_id=request_id,
            control_center_id=payload["control_center_id"],
            public_key_b64=key_pair.public_key_b64,
            public_key_fingerprint=key_pair.fingerprint,
            proof_signature=signature,
        )

    def verify(self) -> None:
        if not _ENROLLMENT_ID_RE.fullmatch(self.enrollment_request_id):
            raise FleetEnrollmentError("invalid enrollment request id")
        expected = public_key_fingerprint(self.public_key_b64)
        if not hmac.compare_digest(expected, self.public_key_fingerprint):
            raise FleetEnrollmentError("Node public-key fingerprint does not match")
        payload = {
            "control_center_id": _clean_control_center_id(self.control_center_id),
            "enrollment_request_id": self.enrollment_request_id,
            "public_key_b64": self.public_key_b64,
            "public_key_fingerprint": self.public_key_fingerprint,
        }
        try:
            verify_signature(
                self.public_key_b64,
                canonical_signed_message("node-enrollment", payload),
                self.proof_signature,
            )
        except FleetIdentityError as exc:
            raise FleetEnrollmentError("Node enrollment proof-of-possession failed") from exc


@dataclass(frozen=True)
class EnrollmentAcknowledgement:
    enrollment_request_id: str
    node_id: str
    control_center_id: str
    public_key_b64: str
    proof_signature: str

    @classmethod
    def create(
        cls,
        *,
        result: "EnrollmentResult",
        key_pair: NodeKeyPair,
    ) -> "EnrollmentAcknowledgement":
        payload = {
            "control_center_id": result.control_center_id,
            "enrollment_request_id": result.enrollment_request_id,
            "node_id": result.node_id,
            "public_key_fingerprint": result.public_key_fingerprint,
        }
        return cls(
            enrollment_request_id=result.enrollment_request_id,
            node_id=result.node_id,
            control_center_id=result.control_center_id,
            public_key_b64=key_pair.public_key_b64,
            proof_signature=key_pair.sign(
                canonical_signed_message("node-enrollment-ack", payload)
            ),
        )


@dataclass(frozen=True)
class EnrollmentResult:
    node_id: str
    enrollment_request_id: str
    control_center_id: str
    public_key_fingerprint: str
    state: str
    created_at: float
    acknowledged_at: Optional[float] = None
    revoked_at: Optional[float] = None


def _clean_control_center_id(value: str) -> str:
    if not isinstance(value, str):
        raise FleetEnrollmentError("control center id must be a string")
    cleaned = value.strip()
    if not cleaned or len(cleaned) > 255 or cleaned != value:
        raise FleetEnrollmentError("control center id must be canonical and non-empty")
    return cleaned


def _bootstrap_digest(control_center_id: str, secret: str) -> str:
    if not isinstance(secret, str) or len(secret) < 32:
        raise FleetEnrollmentError("bootstrap credential secret is invalid")
    raw = (
        b"argus-fleet-bootstrap-v1\0"
        + control_center_id.encode("utf-8")
        + b"\0"
        + secret.encode("utf-8")
    )
    return hashlib.sha256(raw).hexdigest()


class FleetEnrollmentRegistry:
    """SQLite-backed Control Center identity/enrollment registry."""

    def __init__(self, path: Path | str, *, control_center_id: str) -> None:
        self.path = Path(path)
        self.control_center_id = _clean_control_center_id(control_center_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
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
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS fleet_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bootstrap_credentials (
                    credential_id TEXT PRIMARY KEY,
                    control_center_id TEXT NOT NULL,
                    secret_digest TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    created_at REAL NOT NULL,
                    consumed_at REAL,
                    consumed_by_enrollment_request_id TEXT
                );
                CREATE TABLE IF NOT EXISTS nodes (
                    node_id TEXT PRIMARY KEY,
                    control_center_id TEXT NOT NULL,
                    public_key_b64 TEXT NOT NULL,
                    public_key_fingerprint TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    acknowledged_at REAL,
                    revoked_at REAL,
                    revocation_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS enrollments (
                    enrollment_request_id TEXT PRIMARY KEY,
                    control_center_id TEXT NOT NULL,
                    node_id TEXT NOT NULL REFERENCES nodes(node_id),
                    public_key_fingerprint TEXT NOT NULL,
                    state TEXT NOT NULL,
                    bootstrap_credential_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    acknowledged_at REAL,
                    revoked_at REAL
                );
                """
            )
            existing = conn.execute(
                "SELECT value FROM fleet_meta WHERE key='registry_version'"
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO fleet_meta(key, value) VALUES('registry_version', ?)",
                    (FLEET_REGISTRY_VERSION,),
                )
            elif existing["value"] != FLEET_REGISTRY_VERSION:
                raise FleetEnrollmentError("unsupported Fleet registry version")
            audience = conn.execute(
                "SELECT value FROM fleet_meta WHERE key='control_center_id'"
            ).fetchone()
            if audience is None:
                conn.execute(
                    "INSERT INTO fleet_meta(key, value) VALUES('control_center_id', ?)",
                    (self.control_center_id,),
                )
            elif audience["value"] != self.control_center_id:
                raise FleetEnrollmentError(
                    "Fleet registry belongs to a different Control Center"
                )
        finally:
            conn.close()

    def issue_bootstrap_credential(
        self, *, ttl_seconds: float = 600.0, now: Optional[float] = None
    ) -> BootstrapCredential:
        if not isinstance(ttl_seconds, (int, float)) or not (1.0 <= ttl_seconds <= 86400.0):
            raise FleetEnrollmentError("bootstrap credential ttl must be 1..86400 seconds")
        created = float(time.time() if now is None else now)
        credential_id = "BOOT-" + uuid.uuid4().hex
        secret = secrets.token_urlsafe(32)
        expires_at = created + float(ttl_seconds)
        digest = _bootstrap_digest(self.control_center_id, secret)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO bootstrap_credentials(
                    credential_id, control_center_id, secret_digest,
                    expires_at, created_at
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    credential_id,
                    self.control_center_id,
                    digest,
                    expires_at,
                    created,
                ),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
        return BootstrapCredential(
            credential_id=credential_id,
            secret=secret,
            control_center_id=self.control_center_id,
            expires_at=expires_at,
        )

    def enroll(
        self,
        request: EnrollmentRequest,
        *,
        bootstrap_credential_id: Optional[str] = None,
        bootstrap_secret: Optional[str] = None,
        now: Optional[float] = None,
    ) -> EnrollmentResult:
        request.verify()
        if request.control_center_id != self.control_center_id:
            raise FleetEnrollmentError("enrollment request targets a different Control Center")
        current = float(time.time() if now is None else now)

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """
                SELECT e.*, n.public_key_b64, n.state AS node_state,
                       n.acknowledged_at AS node_acknowledged_at,
                       n.revoked_at AS node_revoked_at
                  FROM enrollments e
                  JOIN nodes n ON n.node_id = e.node_id
                 WHERE e.enrollment_request_id = ?
                """,
                (request.enrollment_request_id,),
            ).fetchone()
            if existing is not None:
                result = self._replay_existing(existing, request)
                conn.commit()
                return result

            if (
                bootstrap_credential_id is None
                or bootstrap_secret is None
                or not _BOOTSTRAP_ID_RE.fullmatch(bootstrap_credential_id)
            ):
                raise FleetEnrollmentError(
                    "a valid bootstrap credential is required for first enrollment"
                )

            credential = conn.execute(
                """
                SELECT * FROM bootstrap_credentials
                 WHERE credential_id = ?
                """,
                (bootstrap_credential_id,),
            ).fetchone()
            if credential is None:
                raise FleetEnrollmentError("bootstrap credential is unknown")
            if credential["control_center_id"] != self.control_center_id:
                raise FleetEnrollmentError("bootstrap credential audience mismatch")
            if credential["consumed_at"] is not None:
                raise FleetEnrollmentError("bootstrap credential has already been consumed")
            if current > float(credential["expires_at"]):
                raise FleetEnrollmentError("bootstrap credential has expired")
            expected_digest = _bootstrap_digest(self.control_center_id, bootstrap_secret)
            if not hmac.compare_digest(expected_digest, credential["secret_digest"]):
                raise FleetEnrollmentError("bootstrap credential is invalid")

            fingerprint_owner = conn.execute(
                "SELECT node_id, state FROM nodes WHERE public_key_fingerprint = ?",
                (request.public_key_fingerprint,),
            ).fetchone()
            if fingerprint_owner is not None:
                raise EnrollmentConflict(
                    "Node public key is already bound to another enrollment identity"
                )

            node_id = "NODE-" + uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO nodes(
                    node_id, control_center_id, public_key_b64,
                    public_key_fingerprint, state, created_at
                ) VALUES(?, ?, ?, ?, 'pending_ack', ?)
                """,
                (
                    node_id,
                    self.control_center_id,
                    request.public_key_b64,
                    request.public_key_fingerprint,
                    current,
                ),
            )
            conn.execute(
                """
                INSERT INTO enrollments(
                    enrollment_request_id, control_center_id, node_id,
                    public_key_fingerprint, state,
                    bootstrap_credential_id, created_at
                ) VALUES(?, ?, ?, ?, 'pending_ack', ?, ?)
                """,
                (
                    request.enrollment_request_id,
                    self.control_center_id,
                    node_id,
                    request.public_key_fingerprint,
                    bootstrap_credential_id,
                    current,
                ),
            )
            updated = conn.execute(
                """
                UPDATE bootstrap_credentials
                   SET consumed_at = ?, consumed_by_enrollment_request_id = ?
                 WHERE credential_id = ? AND consumed_at IS NULL
                """,
                (current, request.enrollment_request_id, bootstrap_credential_id),
            )
            if updated.rowcount != 1:
                raise FleetEnrollmentError(
                    "bootstrap credential consumption lost transactional authority"
                )
            conn.commit()
            return EnrollmentResult(
                node_id=node_id,
                enrollment_request_id=request.enrollment_request_id,
                control_center_id=self.control_center_id,
                public_key_fingerprint=request.public_key_fingerprint,
                state="pending_ack",
                created_at=current,
            )
        except FleetEnrollmentError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetEnrollmentError("Fleet enrollment registry transaction failed") from exc
        finally:
            conn.close()

    def _replay_existing(
        self, row: sqlite3.Row, request: EnrollmentRequest
    ) -> EnrollmentResult:
        if row["control_center_id"] != self.control_center_id:
            raise EnrollmentConflict("enrollment request audience changed")
        if row["public_key_fingerprint"] != request.public_key_fingerprint:
            raise EnrollmentConflict("enrollment request id was reused with a different Node key")
        if row["public_key_b64"] != request.public_key_b64:
            raise EnrollmentConflict("Node public key changed for an existing enrollment")
        if row["node_state"] == "revoked" or row["state"] == "revoked":
            raise FleetEnrollmentError("Node identity has been revoked")
        return EnrollmentResult(
            node_id=row["node_id"],
            enrollment_request_id=row["enrollment_request_id"],
            control_center_id=row["control_center_id"],
            public_key_fingerprint=row["public_key_fingerprint"],
            state=row["state"],
            created_at=float(row["created_at"]),
            acknowledged_at=(
                float(row["acknowledged_at"])
                if row["acknowledged_at"] is not None
                else None
            ),
            revoked_at=(
                float(row["revoked_at"]) if row["revoked_at"] is not None else None
            ),
        )

    def acknowledge(
        self,
        acknowledgement: EnrollmentAcknowledgement,
        *,
        now: Optional[float] = None,
    ) -> EnrollmentResult:
        if not _ENROLLMENT_ID_RE.fullmatch(acknowledgement.enrollment_request_id):
            raise FleetEnrollmentError("invalid enrollment request id")
        if not _NODE_ID_RE.fullmatch(acknowledgement.node_id):
            raise FleetEnrollmentError("invalid node id")
        if acknowledgement.control_center_id != self.control_center_id:
            raise FleetEnrollmentError("enrollment acknowledgement audience mismatch")
        current = float(time.time() if now is None else now)

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT e.*, n.public_key_b64, n.state AS node_state,
                       n.acknowledged_at AS node_acknowledged_at,
                       n.revoked_at AS node_revoked_at
                  FROM enrollments e
                  JOIN nodes n ON n.node_id=e.node_id
                 WHERE e.enrollment_request_id=?
                """,
                (acknowledgement.enrollment_request_id,),
            ).fetchone()
            if row is None:
                raise FleetEnrollmentError("enrollment request is unknown")
            if row["node_id"] != acknowledgement.node_id:
                raise EnrollmentConflict("acknowledgement names a different Node")
            if row["public_key_b64"] != acknowledgement.public_key_b64:
                raise EnrollmentConflict("acknowledgement uses a different Node key")
            if row["node_state"] == "revoked" or row["state"] == "revoked":
                raise FleetEnrollmentError("Node identity has been revoked")
            payload = {
                "control_center_id": self.control_center_id,
                "enrollment_request_id": row["enrollment_request_id"],
                "node_id": row["node_id"],
                "public_key_fingerprint": row["public_key_fingerprint"],
            }
            try:
                verify_signature(
                    row["public_key_b64"],
                    canonical_signed_message("node-enrollment-ack", payload),
                    acknowledgement.proof_signature,
                )
            except FleetIdentityError as exc:
                raise FleetEnrollmentError(
                    "Node enrollment acknowledgement proof failed"
                ) from exc

            acknowledged_at = row["acknowledged_at"]
            if acknowledged_at is None:
                acknowledged_at = current
                conn.execute(
                    """
                    UPDATE enrollments
                       SET state='active', acknowledged_at=?
                     WHERE enrollment_request_id=?
                    """,
                    (current, row["enrollment_request_id"]),
                )
                conn.execute(
                    """
                    UPDATE nodes
                       SET state='active', acknowledged_at=?
                     WHERE node_id=?
                    """,
                    (current, row["node_id"]),
                )
            conn.commit()
            return EnrollmentResult(
                node_id=row["node_id"],
                enrollment_request_id=row["enrollment_request_id"],
                control_center_id=row["control_center_id"],
                public_key_fingerprint=row["public_key_fingerprint"],
                state="active",
                created_at=float(row["created_at"]),
                acknowledged_at=float(acknowledged_at),
            )
        except FleetEnrollmentError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetEnrollmentError("Fleet acknowledgement transaction failed") from exc
        finally:
            conn.close()

    def revoke_node(
        self,
        node_id: str,
        *,
        reason: str,
        now: Optional[float] = None,
    ) -> None:
        if not _NODE_ID_RE.fullmatch(node_id):
            raise FleetEnrollmentError("invalid node id")
        if not isinstance(reason, str) or not reason.strip():
            raise FleetEnrollmentError("revocation reason must be non-empty")
        current = float(time.time() if now is None else now)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state FROM nodes WHERE node_id=?", (node_id,)
            ).fetchone()
            if row is None:
                raise FleetEnrollmentError("Node identity is unknown")
            conn.execute(
                """
                UPDATE nodes
                   SET state='revoked', revoked_at=?, revocation_reason=?
                 WHERE node_id=?
                """,
                (current, reason.strip(), node_id),
            )
            conn.execute(
                """
                UPDATE enrollments
                   SET state='revoked', revoked_at=?
                 WHERE node_id=?
                """,
                (current, node_id),
            )
            conn.commit()
        except FleetEnrollmentError:
            conn.rollback()
            raise
        except sqlite3.Error as exc:
            conn.rollback()
            raise FleetEnrollmentError("Fleet revocation transaction failed") from exc
        finally:
            conn.close()
