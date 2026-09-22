from __future__ import annotations

import sqlite3

import pytest

from argus.fleet import (
    EnrollmentAcknowledgement,
    EnrollmentConflict,
    EnrollmentRequest,
    FleetEnrollmentError,
    FleetEnrollmentRegistry,
    NodeKeyPair,
)


def _registry(tmp_path):
    return FleetEnrollmentRegistry(
        tmp_path / "fleet-registry.sqlite3",
        control_center_id="cc://argus-test",
    )


def test_enrollment_is_single_use_and_lost_response_replays_identity(tmp_path):
    registry = _registry(tmp_path)
    credential = registry.issue_bootstrap_credential(ttl_seconds=60, now=1000.0)
    key = NodeKeyPair.generate()
    request = EnrollmentRequest.create(
        control_center_id="cc://argus-test",
        key_pair=key,
    )

    first = registry.enroll(
        request,
        bootstrap_credential_id=credential.credential_id,
        bootstrap_secret=credential.secret,
        now=1001.0,
    )
    replay = registry.enroll(request, now=1002.0)

    assert replay == first
    assert first.state == "pending_ack"
    assert first.node_id.startswith("NODE-")

    other_request = EnrollmentRequest.create(
        control_center_id="cc://argus-test",
        key_pair=NodeKeyPair.generate(),
    )
    with pytest.raises(FleetEnrollmentError, match="already been consumed"):
        registry.enroll(
            other_request,
            bootstrap_credential_id=credential.credential_id,
            bootstrap_secret=credential.secret,
            now=1002.0,
        )


def test_same_request_id_with_different_key_is_protocol_conflict(tmp_path):
    registry = _registry(tmp_path)
    credential = registry.issue_bootstrap_credential(ttl_seconds=60, now=1000.0)
    first_key = NodeKeyPair.generate()
    request_id = "ENROLL-" + ("1" * 32)
    first = EnrollmentRequest.create(
        control_center_id="cc://argus-test",
        key_pair=first_key,
        enrollment_request_id=request_id,
    )
    registry.enroll(
        first,
        bootstrap_credential_id=credential.credential_id,
        bootstrap_secret=credential.secret,
        now=1001.0,
    )

    conflicting = EnrollmentRequest.create(
        control_center_id="cc://argus-test",
        key_pair=NodeKeyPair.generate(),
        enrollment_request_id=request_id,
    )
    with pytest.raises(EnrollmentConflict, match="different Node key"):
        registry.enroll(conflicting, now=1002.0)


def test_enrollment_proof_detects_tampering(tmp_path):
    registry = _registry(tmp_path)
    credential = registry.issue_bootstrap_credential(ttl_seconds=60, now=1000.0)
    key = NodeKeyPair.generate()
    request = EnrollmentRequest.create(
        control_center_id="cc://argus-test",
        key_pair=key,
    )
    tampered = EnrollmentRequest(
        enrollment_request_id=request.enrollment_request_id,
        control_center_id=request.control_center_id,
        public_key_b64=NodeKeyPair.generate().public_key_b64,
        public_key_fingerprint=request.public_key_fingerprint,
        proof_signature=request.proof_signature,
    )

    with pytest.raises(FleetEnrollmentError, match="fingerprint does not match"):
        registry.enroll(
            tampered,
            bootstrap_credential_id=credential.credential_id,
            bootstrap_secret=credential.secret,
            now=1001.0,
        )


def test_expired_bootstrap_fails_closed(tmp_path):
    registry = _registry(tmp_path)
    credential = registry.issue_bootstrap_credential(ttl_seconds=10, now=1000.0)
    request = EnrollmentRequest.create(
        control_center_id="cc://argus-test",
        key_pair=NodeKeyPair.generate(),
    )

    with pytest.raises(FleetEnrollmentError, match="expired"):
        registry.enroll(
            request,
            bootstrap_credential_id=credential.credential_id,
            bootstrap_secret=credential.secret,
            now=1010.01,
        )


def test_acknowledgement_activates_identity_and_is_idempotent(tmp_path):
    registry = _registry(tmp_path)
    credential = registry.issue_bootstrap_credential(ttl_seconds=60, now=1000.0)
    key = NodeKeyPair.generate()
    request = EnrollmentRequest.create(
        control_center_id="cc://argus-test",
        key_pair=key,
    )
    pending = registry.enroll(
        request,
        bootstrap_credential_id=credential.credential_id,
        bootstrap_secret=credential.secret,
        now=1001.0,
    )
    ack = EnrollmentAcknowledgement.create(result=pending, key_pair=key)

    active = registry.acknowledge(ack, now=1002.0)
    replay = registry.acknowledge(ack, now=1003.0)

    assert active.state == "active"
    assert active.acknowledged_at == 1002.0
    assert replay == active


def test_registry_restart_recovers_pending_enrollment_without_bootstrap(tmp_path):
    path = tmp_path / "fleet-registry.sqlite3"
    registry = FleetEnrollmentRegistry(path, control_center_id="cc://argus-test")
    credential = registry.issue_bootstrap_credential(ttl_seconds=60, now=1000.0)
    key = NodeKeyPair.generate()
    request = EnrollmentRequest.create(
        control_center_id="cc://argus-test",
        key_pair=key,
    )
    first = registry.enroll(
        request,
        bootstrap_credential_id=credential.credential_id,
        bootstrap_secret=credential.secret,
        now=1001.0,
    )

    reopened = FleetEnrollmentRegistry(path, control_center_id="cc://argus-test")
    recovered = reopened.enroll(request, now=5000.0)

    assert recovered == first


def test_revoked_pending_identity_cannot_be_recovered(tmp_path):
    registry = _registry(tmp_path)
    credential = registry.issue_bootstrap_credential(ttl_seconds=60, now=1000.0)
    key = NodeKeyPair.generate()
    request = EnrollmentRequest.create(
        control_center_id="cc://argus-test",
        key_pair=key,
    )
    result = registry.enroll(
        request,
        bootstrap_credential_id=credential.credential_id,
        bootstrap_secret=credential.secret,
        now=1001.0,
    )
    registry.revoke_node(result.node_id, reason="operator abandoned join", now=1002.0)

    with pytest.raises(FleetEnrollmentError, match="revoked"):
        registry.enroll(request, now=1003.0)


def test_bootstrap_secret_is_not_persisted_in_registry(tmp_path):
    registry = _registry(tmp_path)
    credential = registry.issue_bootstrap_credential(ttl_seconds=60, now=1000.0)
    request = EnrollmentRequest.create(
        control_center_id="cc://argus-test",
        key_pair=NodeKeyPair.generate(),
    )
    registry.enroll(
        request,
        bootstrap_credential_id=credential.credential_id,
        bootstrap_secret=credential.secret,
        now=1001.0,
    )

    conn = sqlite3.connect(registry.path)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    assert credential.secret.encode("utf-8") not in registry.path.read_bytes()


def test_node_key_round_trip_uses_restricted_file_on_posix(tmp_path):
    key = NodeKeyPair.generate()
    path = key.save(tmp_path / "identity" / "node-key.json")
    loaded = NodeKeyPair.load(path)

    assert loaded.public_key_b64 == key.public_key_b64
    assert loaded.fingerprint == key.fingerprint
