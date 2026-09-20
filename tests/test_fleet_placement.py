from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from argus.ates import RunId
from argus.fleet.enrollment import (
    EnrollmentAcknowledgement,
    EnrollmentRequest,
    FleetEnrollmentRegistry,
)
from argus.fleet.identity import ControlCenterKeyPair, NodeKeyPair
from argus.fleet.placement import (
    CapacityUnavailable,
    FleetPlacementError,
    FleetPlacementStore,
    NodeAdmissionStore,
    PlacementConflict,
    SessionRequest,
    StagedInputIdentity,
)


def _enroll(registry, *, now):
    credential = registry.issue_bootstrap_credential(now=now, ttl_seconds=60)
    key = NodeKeyPair.generate()
    request = EnrollmentRequest.create(control_center_id="cc://test", key_pair=key)
    pending = registry.enroll(
        request,
        bootstrap_credential_id=credential.credential_id,
        bootstrap_secret=credential.secret,
        now=now + 1,
    )
    active = registry.acknowledge(
        EnrollmentAcknowledgement.create(result=pending, key_pair=key),
        now=now + 2,
    )
    return active, key


def _setup(tmp_path):
    registry_path = tmp_path / "control.sqlite3"
    registry = FleetEnrollmentRegistry(registry_path, control_center_id="cc://test")
    node1, key1 = _enroll(registry, now=100)
    node2, key2 = _enroll(registry, now=200)
    control_key = ControlCenterKeyPair.generate()
    placements = FleetPlacementStore(registry_path, control_center_id="cc://test")
    return registry_path, placements, control_key, (node1, key1), (node2, key2)


def _request(*, session=None, run=None, sensitive=False):
    staged = StagedInputIdentity(
        logical_name="application-under-test",
        size_bytes=1234,
        transfer_digest="sha256:" + ("b" * 64),
        provenance_method="hmac-sha256" if sensitive else "sha256",
        provenance_value=("hmac:" + ("c" * 64)) if sensitive else ("sha256:" + ("b" * 64)),
        sensitive=sensitive,
    )
    return SessionRequest.create(
        session_request_id=session,
        run_id=run or RunId.new(),
        image_digest="sha256:" + ("a" * 64),
        spec_digest="sha256:" + ("d" * 64),
        provider="hyperv",
        guest_os="windows",
        staged_inputs=(staged,),
        network_policy={"mode": "host_only"},
        isolation_policy={"secure": True},
    )


def _node_store(tmp_path, node, control_key, *, max_sessions=1):
    return NodeAdmissionStore(
        tmp_path / f"{node.node_id}.sqlite3",
        node_id=node.node_id,
        control_center_id="cc://test",
        control_center_public_key_b64=control_key.public_key_b64,
        max_sessions=max_sessions,
    )


def test_global_placement_is_idempotent_and_conflicting_request_fails(tmp_path):
    _, placements, _, (node1, _), _ = _setup(tmp_path)
    request = _request()
    first = placements.create_placement(request, owner_node_id=node1.node_id, now=1000)
    replay = placements.create_placement(request, owner_node_id=node1.node_id, now=1001)
    assert replay == first

    altered = _request(session=request.session_request_id, run=request.run_id)
    altered = SessionRequest.create(
        session_request_id=altered.session_request_id,
        run_id=altered.run_id,
        image_digest="sha256:" + ("e" * 64),
        spec_digest=altered.spec_digest,
        provider=altered.provider,
        guest_os=altered.guest_os,
        staged_inputs=altered.staged_inputs,
        network_policy=altered.network_policy,
        isolation_policy=altered.isolation_policy,
    )
    with pytest.raises(PlacementConflict, match="different immutable"):
        placements.create_placement(altered, owner_node_id=node1.node_id, now=1002)


def test_cancellation_commits_before_dispatch_authority_and_is_idempotent(tmp_path):
    _, placements, control_key, (node1, _), _ = _setup(tmp_path)
    request = _request()
    record = placements.create_placement(request, owner_node_id=node1.node_id, now=1000)
    assert placements.authorization_for_dispatch(request.session_request_id, signer=control_key)

    op = "CANCEL-" + ("1" * 32)
    cancelled = placements.request_cancellation(
        request.session_request_id,
        cancellation_operation_id=op,
        now=1001,
    )
    replay = placements.request_cancellation(
        request.session_request_id,
        cancellation_operation_id=op,
        now=1002,
    )
    assert cancelled == replay
    assert cancelled.state == "cancellation_pending"
    with pytest.raises(FleetPlacementError, match="not start-dispatchable"):
        placements.authorization_for_dispatch(request.session_request_id, signer=control_key)


def test_ownership_cannot_move_on_heartbeat_loss_only(tmp_path):
    _, placements, _, (node1, _), (node2, _) = _setup(tmp_path)
    request = _request()
    record = placements.create_placement(request, owner_node_id=node1.node_id, now=1000)

    with pytest.raises(FleetPlacementError, match="requires definitive"):
        placements.transfer_ownership(request.session_request_id, new_owner_node_id=node2.node_id, now=2000)

    placements.record_definitive_fence(
        request.session_request_id,
        placement_generation=record.placement_generation,
        method="provider_termination_and_tombstone",
        evidence_digest="sha256:" + ("f" * 64),
        fence_operation_id="FENCE-" + ("2" * 32),
    )
    moved = placements.transfer_ownership(
        request.session_request_id,
        new_owner_node_id=node2.node_id,
        now=2001,
    )
    assert moved.owner_node_id == node2.node_id
    assert moved.placement_generation == 2


def test_node_capacity_reservation_is_atomic_and_retry_does_not_consume_twice(tmp_path):
    _, placements, control_key, (node1, _), _ = _setup(tmp_path)
    store = _node_store(tmp_path, node1, control_key, max_sessions=1)
    request1 = _request()
    p1 = placements.create_placement(request1, owner_node_id=node1.node_id, now=1000)
    a1 = placements.authorization_for_dispatch(request1.session_request_id, signer=control_key)
    first = store.admit(request1, a1)
    replay = store.admit(request1, a1)
    assert first == replay

    request2 = _request()
    placements.create_placement(request2, owner_node_id=node1.node_id, now=1001)
    a2 = placements.authorization_for_dispatch(request2.session_request_id, signer=control_key)
    with pytest.raises(CapacityUnavailable):
        store.admit(request2, a2)


def test_concurrent_node_admission_cannot_overcommit_one_slot(tmp_path):
    _, placements, control_key, (node1, _), _ = _setup(tmp_path)
    store = _node_store(tmp_path, node1, control_key, max_sessions=1)
    requests = [_request(), _request()]
    auths = []
    for index, request in enumerate(requests):
        placements.create_placement(request, owner_node_id=node1.node_id, now=1000 + index)
        auths.append(placements.authorization_for_dispatch(request.session_request_id, signer=control_key))

    def attempt(index):
        try:
            return store.admit(requests[index], auths[index]).state
        except CapacityUnavailable:
            return "capacity_unavailable"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (0, 1)))
    assert sorted(results) == ["capacity_unavailable", "reserved"]


def test_cancel_before_allocation_tombstone_blocks_delayed_authorized_request(tmp_path):
    _, placements, control_key, (node1, _), _ = _setup(tmp_path)
    store = _node_store(tmp_path, node1, control_key)
    request = _request()
    placement = placements.create_placement(request, owner_node_id=node1.node_id, now=1000)
    auth = placements.authorization_for_dispatch(request.session_request_id, signer=control_key)
    op = "CANCEL-" + ("3" * 32)
    placements.request_cancellation(request.session_request_id, cancellation_operation_id=op, now=1001)

    pre = store.cancel(
        request.session_request_id,
        placement_generation=placement.placement_generation,
        cancellation_operation_id=op,
    )
    assert pre.state == "cancelled"
    delayed = store.admit(request, auth)
    assert delayed.state == "cancelled"


def test_terminal_tombstone_survives_restart_and_requires_retirement_before_gc(tmp_path):
    _, placements, control_key, (node1, _), _ = _setup(tmp_path)
    path = tmp_path / f"{node1.node_id}.sqlite3"
    store = _node_store(tmp_path, node1, control_key)
    request = _request()
    placement = placements.create_placement(request, owner_node_id=node1.node_id, now=1000)
    auth = placements.authorization_for_dispatch(request.session_request_id, signer=control_key)
    store.admit(request, auth)
    store.transition(request.session_request_id, placement_generation=1, new_state="allocated")
    store.transition(request.session_request_id, placement_generation=1, new_state="starting")
    store.transition(request.session_request_id, placement_generation=1, new_state="running")
    terminal = store.mark_terminal(request.session_request_id, placement_generation=1, terminal_state="completed")
    assert terminal.state == "completed"

    reopened = NodeAdmissionStore(
        path,
        node_id=node1.node_id,
        control_center_id="cc://test",
        control_center_public_key_b64=control_key.public_key_b64,
        max_sessions=1,
    )
    replay = reopened.admit(request, auth)
    assert replay.state == "completed"
    with pytest.raises(FleetPlacementError, match="before generation retirement"):
        reopened.garbage_collect_tombstone(request.session_request_id, placement_generation=1)

    placements.mark_terminal(
        request.session_request_id,
        owner_node_id=node1.node_id,
        placement_generation=1,
        terminal_state="completed",
        now=1010,
    )
    retirement = placements.retire_generation(
        request.session_request_id,
        placement_generation=1,
        signer=control_key,
        now=1011,
    )
    retired = reopened.retire_generation(retirement)
    assert retired.retired
    reopened.garbage_collect_tombstone(request.session_request_id, placement_generation=1)
    with pytest.raises(FleetPlacementError, match="retired placement generation"):
        reopened.admit(request, auth)


def test_stale_generation_authorization_cannot_be_used_on_new_owner(tmp_path):
    _, placements, control_key, (node1, _), (node2, _) = _setup(tmp_path)
    request = _request()
    first = placements.create_placement(request, owner_node_id=node1.node_id, now=1000)
    stale = placements.authorization_for_dispatch(request.session_request_id, signer=control_key)
    placements.record_definitive_fence(
        request.session_request_id,
        placement_generation=1,
        method="host_execution_disabled",
        evidence_digest="sha256:" + ("f" * 64),
    )
    moved = placements.transfer_ownership(request.session_request_id, new_owner_node_id=node2.node_id, now=1001)
    store2 = _node_store(tmp_path, node2, control_key)
    with pytest.raises(PlacementConflict, match="does not bind this exact Node"):
        store2.admit(request, stale)
    fresh = placements.authorization_for_dispatch(request.session_request_id, signer=control_key)
    assert fresh.placement_generation == moved.placement_generation == 2
    assert store2.admit(request, fresh).state == "reserved"


def test_sensitive_staged_input_cannot_publish_raw_sha256_provenance():
    with pytest.raises(FleetPlacementError, match="sensitive staged input"):
        StagedInputIdentity(
            logical_name="license-secret",
            size_bytes=20,
            transfer_digest="sha256:" + ("a" * 64),
            provenance_method="sha256",
            provenance_value="sha256:" + ("a" * 64),
            sensitive=True,
        )


def test_session_request_policy_input_is_copied_into_immutable_canonical_identity():
    network = {"mode": "host_only", "allow": ["10.0.0.0/8"]}
    request = SessionRequest.create(
        run_id=RunId.new(),
        image_digest="sha256:" + ("a" * 64),
        spec_digest="sha256:" + ("d" * 64),
        provider="hyperv",
        guest_os="windows",
        network_policy=network,
        isolation_policy={"secure": True},
    )
    before = request.request_digest
    network["mode"] = "open"
    network["allow"].append("0.0.0.0/0")
    assert request.request_digest == before
    assert request.network_policy == {"allow": ["10.0.0.0/8"], "mode": "host_only"}


def test_heartbeat_or_lease_style_fence_reason_is_rejected(tmp_path):
    _, placements, _, (node1, _), _ = _setup(tmp_path)
    request = _request()
    placements.create_placement(request, owner_node_id=node1.node_id, now=1000)
    with pytest.raises(FleetPlacementError, match="non-executable"):
        placements.record_definitive_fence(
            request.session_request_id,
            placement_generation=1,
            method="heartbeat_expired",
            evidence_digest="sha256:" + ("f" * 64),
        )
