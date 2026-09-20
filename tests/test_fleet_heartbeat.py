from __future__ import annotations

import pytest

from argus.fleet.enrollment import (
    EnrollmentAcknowledgement,
    EnrollmentRequest,
    FleetEnrollmentRegistry,
)
from argus.fleet.heartbeat import (
    CapacityAdvertisement,
    ClockProbeResponse,
    FleetHeartbeatError,
    FleetHeartbeatRegistry,
    HeartbeatConflict,
    ImageAdvertisement,
    NodeHeartbeat,
)
from argus.fleet.identity import NodeKeyPair


def _active_node(tmp_path):
    path = tmp_path / "fleet.sqlite3"
    enrollment = FleetEnrollmentRegistry(
        path,
        control_center_id="cc://test",
    )
    credential = enrollment.issue_bootstrap_credential(
        now=100.0,
        ttl_seconds=60,
    )
    key = NodeKeyPair.generate()
    request = EnrollmentRequest.create(
        control_center_id="cc://test",
        key_pair=key,
    )
    pending = enrollment.enroll(
        request,
        bootstrap_credential_id=credential.credential_id,
        bootstrap_secret=credential.secret,
        now=101.0,
    )
    active = enrollment.acknowledge(
        EnrollmentAcknowledgement.create(
            result=pending,
            key_pair=key,
        ),
        now=102.0,
    )
    return path, active, key


def _heartbeat(
    active,
    key,
    *,
    sequence=1,
    boot_id=None,
    **kwargs,
):
    image = ImageAdvertisement(
        alias="win11-qa",
        image_id="IMG-WIN11-QA-2026-09",
        digest="sha256:" + ("a" * 64),
        guest_os="windows",
    )
    return NodeHeartbeat.create(
        node_id=active.node_id,
        control_center_id="cc://test",
        key_pair=key,
        boot_id=boot_id or ("BOOTID-" + ("1" * 32)),
        sequence=sequence,
        agent_version="0.1.0",
        host_os="windows",
        provider="hyperv",
        capacity=CapacityAdvertisement(
            max_sessions=4,
            active_sessions=1,
            memory_mb=32768,
            available_memory_mb=20000,
            disk_free_mb=100000,
        ),
        images=(image,),
        active_session_ids=("SESSION-abc",),
        node_time=1000.0 + sequence,
        monotonic_seconds=50.0 + sequence,
        **kwargs,
    )


def test_authenticated_heartbeat_records_control_receipt_separately(
    tmp_path,
):
    path, active, key = _active_node(tmp_path)
    registry = FleetHeartbeatRegistry(
        path,
        control_center_id="cc://test",
    )
    heartbeat = _heartbeat(active, key)

    receipt = registry.accept_heartbeat(
        heartbeat,
        received_at=2000.25,
    )

    assert receipt.node_id == active.node_id
    assert receipt.received_at == 2000.25
    assert (
        registry.node_health(
            active.node_id,
            now=2001.0,
        ).state
        == "healthy"
    )


def test_heartbeat_replay_is_idempotent_but_conflicting_id_is_rejected(
    tmp_path,
):
    path, active, key = _active_node(tmp_path)
    registry = FleetHeartbeatRegistry(
        path,
        control_center_id="cc://test",
    )
    heartbeat = _heartbeat(active, key)
    first = registry.accept_heartbeat(
        heartbeat,
        received_at=2000.0,
    )
    replay = registry.accept_heartbeat(
        heartbeat,
        received_at=3000.0,
    )
    assert replay == first

    conflict = NodeHeartbeat.create(
        node_id=active.node_id,
        control_center_id="cc://test",
        key_pair=key,
        heartbeat_id=heartbeat.heartbeat_id,
        boot_id=heartbeat.boot_id,
        sequence=2,
        agent_version="0.1.0",
        host_os="windows",
        provider="hyperv",
        capacity=heartbeat.capacity,
        images=heartbeat.images,
        node_time=1002.0,
        monotonic_seconds=52.0,
    )
    with pytest.raises(HeartbeatConflict, match="reused"):
        registry.accept_heartbeat(
            conflict,
            received_at=2001.0,
        )


def test_stale_sequence_is_rejected_within_same_boot_but_new_boot_can_restart(
    tmp_path,
):
    path, active, key = _active_node(tmp_path)
    registry = FleetHeartbeatRegistry(
        path,
        control_center_id="cc://test",
    )
    registry.accept_heartbeat(
        _heartbeat(active, key, sequence=2),
        received_at=2000.0,
    )

    with pytest.raises(FleetHeartbeatError, match="stale"):
        registry.accept_heartbeat(
            _heartbeat(active, key, sequence=1),
            received_at=2001.0,
        )

    new_boot = "BOOTID-" + ("2" * 32)
    receipt = registry.accept_heartbeat(
        _heartbeat(
            active,
            key,
            sequence=1,
            boot_id=new_boot,
        ),
        received_at=2002.0,
    )
    assert receipt.node_id == active.node_id


def test_heartbeat_must_use_enrolled_active_key(tmp_path):
    path, active, _ = _active_node(tmp_path)
    registry = FleetHeartbeatRegistry(
        path,
        control_center_id="cc://test",
    )
    wrong = NodeKeyPair.generate()

    with pytest.raises(
        HeartbeatConflict,
        match="does not match enrolled",
    ):
        registry.accept_heartbeat(
            _heartbeat(active, wrong),
            received_at=2000.0,
        )


def test_degraded_and_disconnected_are_visibility_states_not_fencing(
    tmp_path,
):
    path, active, key = _active_node(tmp_path)
    registry = FleetHeartbeatRegistry(
        path,
        control_center_id="cc://test",
    )
    heartbeat = _heartbeat(
        active,
        key,
        degraded_conditions=("disk_pressure",),
        sync_status="degraded",
    )
    registry.accept_heartbeat(
        heartbeat,
        received_at=2000.0,
    )

    degraded = registry.node_health(
        active.node_id,
        now=2001.0,
    )
    disconnected = registry.node_health(
        active.node_id,
        now=2020.0,
        disconnected_after_seconds=15,
    )
    assert degraded.state == "degraded"
    assert degraded.degraded_conditions == ("disk_pressure",)
    assert disconnected.state == "disconnected"


def test_image_alias_requires_immutable_digest_and_duplicate_aliases_fail(
    tmp_path,
):
    with pytest.raises(FleetHeartbeatError, match="sha256"):
        ImageAdvertisement(
            "win",
            "IMG-1",
            "latest",
            "windows",
        )

    _, active, key = _active_node(tmp_path)
    image = ImageAdvertisement(
        "win",
        "IMG-1",
        "sha256:" + ("a" * 64),
        "windows",
    )
    with pytest.raises(
        FleetHeartbeatError,
        match="aliases must be unique",
    ):
        NodeHeartbeat.create(
            node_id=active.node_id,
            control_center_id="cc://test",
            key_pair=key,
            boot_id="BOOTID-" + ("1" * 32),
            sequence=1,
            agent_version="0.1.0",
            host_os="windows",
            provider="hyperv",
            capacity=CapacityAdvertisement(
                2,
                0,
                1000,
                900,
                5000,
            ),
            images=(image, image),
            node_time=1.0,
            monotonic_seconds=1.0,
        )


def test_clock_probe_computes_offset_and_uncertainty_from_round_trip(
    tmp_path,
):
    path, active, key = _active_node(tmp_path)
    registry = FleetHeartbeatRegistry(
        path,
        control_center_id="cc://test",
    )
    challenge = registry.begin_clock_probe(
        active.node_id,
        control_sent_at=1000.000,
    )

    # Control clock is 0.100s behind Node clock. Network is 20ms each
    # direction and Node processing is 10ms. The NTP-style offset is
    # therefore +100ms, with 40ms network RTT and a +/-20ms bound.
    response = ClockProbeResponse.create(
        challenge=challenge,
        key_pair=key,
        boot_id="BOOTID-" + ("1" * 32),
        node_received_at=1000.120,
        node_sent_at=1000.130,
        node_monotonic_seconds=50.0,
    )
    assessment = registry.complete_clock_probe(
        response,
        control_received_at=1000.050,
    )

    assert assessment.node_minus_control_offset_ms == pytest.approx(
        100.0,
        abs=0.001,
    )
    assert assessment.round_trip_ms == pytest.approx(
        40.0,
        abs=0.001,
    )
    assert assessment.uncertainty_ms == pytest.approx(
        20.0,
        abs=0.001,
    )
    latest = registry.latest_clock_assessment(
        active.node_id,
        now=1001.050,
    )
    assert latest is not None
    assert latest.sample_age_ms == pytest.approx(
        1000.0,
        abs=0.001,
    )


def test_clock_probe_replay_is_idempotent_and_tamper_conflicts(
    tmp_path,
):
    path, active, key = _active_node(tmp_path)
    registry = FleetHeartbeatRegistry(
        path,
        control_center_id="cc://test",
    )
    challenge = registry.begin_clock_probe(
        active.node_id,
        control_sent_at=1000.0,
    )
    response = ClockProbeResponse.create(
        challenge=challenge,
        key_pair=key,
        boot_id="BOOTID-" + ("1" * 32),
        node_received_at=1000.02,
        node_sent_at=1000.03,
        node_monotonic_seconds=50.0,
    )
    first = registry.complete_clock_probe(
        response,
        control_received_at=1000.05,
    )
    replay = registry.complete_clock_probe(
        response,
        control_received_at=1001.05,
    )
    assert replay.probe_id == first.probe_id
    assert (
        replay.node_minus_control_offset_ms
        == first.node_minus_control_offset_ms
    )
    assert replay.sample_age_ms == pytest.approx(
        1000.0,
        abs=0.001,
    )

    altered = ClockProbeResponse.create(
        challenge=challenge,
        key_pair=key,
        boot_id="BOOTID-" + ("1" * 32),
        node_received_at=1000.03,
        node_sent_at=1000.04,
        node_monotonic_seconds=50.1,
    )
    with pytest.raises(
        HeartbeatConflict,
        match="conflicting response",
    ):
        registry.complete_clock_probe(
            altered,
            control_received_at=1001.06,
        )
