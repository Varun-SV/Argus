from __future__ import annotations

import pytest

from argus.fleet.enrollment import (
    EnrollmentAcknowledgement,
    EnrollmentRequest,
    FleetEnrollmentRegistry,
)
from argus.fleet.heartbeat import (
    CapacityAdvertisement,
    FleetHeartbeatError,
    FleetHeartbeatRegistry,
    NodeHeartbeat,
)
from argus.fleet.identity import NodeKeyPair


def _active_node(tmp_path):
    path = tmp_path / "fleet.sqlite3"
    enrollment = FleetEnrollmentRegistry(path, control_center_id="cc://test")
    credential = enrollment.issue_bootstrap_credential(now=100.0, ttl_seconds=60)
    key = NodeKeyPair.generate()
    request = EnrollmentRequest.create(control_center_id="cc://test", key_pair=key)
    pending = enrollment.enroll(
        request,
        bootstrap_credential_id=credential.credential_id,
        bootstrap_secret=credential.secret,
        now=101.0,
    )
    active = enrollment.acknowledge(
        EnrollmentAcknowledgement.create(result=pending, key_pair=key), now=102.0
    )
    return path, active, key


def _heartbeat(
    active,
    key,
    *,
    boot_id,
    sequence,
    active_sessions,
    previous_boot_id=None,
):
    return NodeHeartbeat.create(
        node_id=active.node_id,
        control_center_id="cc://test",
        key_pair=key,
        boot_id=boot_id,
        previous_boot_id=previous_boot_id,
        sequence=sequence,
        agent_version="0.1.0",
        host_os="windows",
        provider="hyperv",
        capacity=CapacityAdvertisement(
            max_sessions=4,
            active_sessions=len(active_sessions),
            memory_mb=32768,
            available_memory_mb=20000,
            disk_free_mb=100000,
        ),
        images=(),
        active_session_ids=active_sessions,
        node_time=1000.0 + sequence,
        monotonic_seconds=50.0 + sequence,
    )


def test_retired_boot_cannot_replace_newer_boot_latest_state(tmp_path):
    path, active, key = _active_node(tmp_path)
    registry = FleetHeartbeatRegistry(path, control_center_id="cc://test")
    old_boot = "BOOTID-" + ("1" * 32)
    new_boot = "BOOTID-" + ("2" * 32)
    registry.accept_heartbeat(
        _heartbeat(active, key, boot_id=old_boot, sequence=1, active_sessions=("SESSION-old",)),
        received_at=2000.0,
    )
    registry.accept_heartbeat(
        _heartbeat(
            active,
            key,
            boot_id=new_boot,
            previous_boot_id=old_boot,
            sequence=1,
            active_sessions=("SESSION-new",),
        ),
        received_at=2001.0,
    )
    with pytest.raises(FleetHeartbeatError):
        registry.accept_heartbeat(
            _heartbeat(active, key, boot_id=old_boot, sequence=2, active_sessions=("SESSION-stale",)),
            received_at=2002.0,
        )
    assert registry.node_health(active.node_id, now=2002.0).last_received_at == 2001.0
    reopened = FleetHeartbeatRegistry(path, control_center_id="cc://test")
    with pytest.raises(FleetHeartbeatError):
        reopened.accept_heartbeat(
            _heartbeat(active, key, boot_id=old_boot, sequence=3, active_sessions=("SESSION-stale",)),
            received_at=2003.0,
        )


def test_delayed_first_heartbeat_from_unseen_old_boot_cannot_replace_current(tmp_path):
    path, active, key = _active_node(tmp_path)
    registry = FleetHeartbeatRegistry(path, control_center_id="cc://test")
    predecessor = "BOOTID-" + ("0" * 32)
    current = "BOOTID-" + ("2" * 32)
    delayed_old = "BOOTID-" + ("1" * 32)

    # The Control Center first sees the predecessor, then an authenticated restart.
    registry.accept_heartbeat(
        _heartbeat(active, key, boot_id=predecessor, sequence=1, active_sessions=()),
        received_at=2000.0,
    )
    registry.accept_heartbeat(
        _heartbeat(
            active,
            key,
            boot_id=current,
            previous_boot_id=predecessor,
            sequence=1,
            active_sessions=("SESSION-current",),
        ),
        received_at=2001.0,
    )

    # This boot has never been accepted, so history-based retirement alone cannot
    # identify it as stale. Its signed transition proof names the old predecessor,
    # not the current boot, and must therefore fail closed.
    with pytest.raises(FleetHeartbeatError, match="does not prove transition"):
        registry.accept_heartbeat(
            _heartbeat(
                active,
                key,
                boot_id=delayed_old,
                previous_boot_id=predecessor,
                sequence=1,
                active_sessions=("SESSION-stale",),
            ),
            received_at=2002.0,
        )

    assert registry.node_health(active.node_id, now=2002.0).last_received_at == 2001.0
