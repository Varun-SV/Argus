from __future__ import annotations

import hashlib

import pytest

from argus.adapters.base import Observation
from argus.ates import RunId
from argus.execution.base import ExecutionEnvironment
from argus.fleet.enrollment import EnrollmentAcknowledgement, EnrollmentRequest, FleetEnrollmentRegistry
from argus.fleet.execution import FleetNodeExecutor
from argus.fleet.identity import ControlCenterKeyPair, NodeKeyPair
from argus.fleet.placement import (
    FleetPlacementError,
    FleetPlacementStore,
    NodeAdmissionStore,
    SessionRequest,
    StagedInputIdentity,
)


class _LostResponseCapsule(ExecutionEnvironment):
    environment_type = "capsule"
    isolated = True
    location = "test-capsule"
    type_name = "test"

    def __init__(self, launches: list[str], launched_keys: set[str], execution_key: str) -> None:
        self.launches = launches
        self.launched_keys = launched_keys
        self.execution_key = execution_key

    def launch(self, target: str) -> None:
        if self.execution_key in self.launched_keys:
            return
        self.launched_keys.add(self.execution_key)
        self.launches.append(target)
        raise RuntimeError("launch response lost")

    def observe(self, include_screenshot: bool = True) -> Observation:
        return Observation(window_title="fake")

    def act(self, action: dict) -> str:
        return "ok"

    def close(self) -> None:
        pass


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _setup(tmp_path):
    registry_path = tmp_path / "control.sqlite3"
    registry = FleetEnrollmentRegistry(registry_path, control_center_id="cc://test")
    credential = registry.issue_bootstrap_credential(now=100, ttl_seconds=60)
    node_key = NodeKeyPair.generate()
    enrollment = EnrollmentRequest.create(control_center_id="cc://test", key_pair=node_key)
    pending = registry.enroll(
        enrollment,
        bootstrap_credential_id=credential.credential_id,
        bootstrap_secret=credential.secret,
        now=101,
    )
    node = registry.acknowledge(
        EnrollmentAcknowledgement.create(result=pending, key_pair=node_key), now=102
    )
    control_key = ControlCenterKeyPair.generate()
    placements = FleetPlacementStore(registry_path, control_center_id="cc://test")
    admissions = NodeAdmissionStore(
        tmp_path / "node.sqlite3",
        node_id=node.node_id,
        control_center_id="cc://test",
        control_center_public_key_b64=control_key.public_key_b64,
        max_sessions=1,
    )
    image_bytes = b"immutable capsule image"
    input_bytes = b"immutable staged input"
    image = tmp_path / "image.vhdx"
    staged = tmp_path / "input.bin"
    image.write_bytes(image_bytes)
    staged.write_bytes(input_bytes)
    identity = StagedInputIdentity(
        logical_name="input.bin",
        size_bytes=len(input_bytes),
        transfer_digest=_sha(input_bytes),
        provenance_method="sha256",
        provenance_value=_sha(input_bytes),
    )
    request = SessionRequest.create(
        run_id=RunId.new(),
        image_digest=_sha(image_bytes),
        spec_digest=_sha(b"spec"),
        provider="fake",
        guest_os="windows",
        staged_inputs=(identity,),
        network_policy={"mode": "host_only"},
        isolation_policy={"secure": True},
    )
    placements.create_placement(request, owner_node_id=node.node_id, now=1000)
    authorization = placements.authorization_for_dispatch(request.session_request_id, signer=control_key)
    return control_key, placements, admissions, request, authorization, image, staged


def _launch_ambiguously(tmp_path, admissions, request, authorization, image, staged, *, observed="completed"):
    launches: list[str] = []
    launched_keys: set[str] = set()

    def factory(_request, _verified, execution_key):
        return _LostResponseCapsule(launches, launched_keys, execution_key)

    executor = FleetNodeExecutor(
        tmp_path / "execution.sqlite3",
        admission_store=admissions,
        environment_factory=factory,
        image_path_resolver=lambda _request: image,
        staged_path_resolver=lambda _identity: staged,
        execution_probe=lambda _key: observed,
    )
    with pytest.raises(RuntimeError, match="launch response lost"):
        executor.dispatch(request, authorization, target="app.exe")
    return executor, launches, factory


def test_cancellation_wins_provider_completion_and_releases_capacity(tmp_path):
    control_key, placements, admissions, request, authorization, image, staged = _setup(tmp_path)
    executor, launches, _ = _launch_ambiguously(
        tmp_path, admissions, request, authorization, image, staged
    )
    admissions.cancel(
        request.session_request_id,
        placement_generation=authorization.placement_generation,
        cancellation_operation_id="CANCEL-00000000000000000000000000000001",
    )

    reconciled = executor.reconcile(
        request.session_request_id,
        placement_generation=authorization.placement_generation,
    )
    assert reconciled.state == "cancelled"
    assert launches == ["app.exe"]

    request2 = SessionRequest.create(
        run_id=RunId.new(),
        image_digest=request.image_digest,
        spec_digest=request.spec_digest,
        provider=request.provider,
        guest_os=request.guest_os,
        staged_inputs=request.staged_inputs,
        network_policy=request.network_policy,
        isolation_policy=request.isolation_policy,
    )
    placements.create_placement(request2, owner_node_id=authorization.owner_node_id, now=1001)
    authorization2 = placements.authorization_for_dispatch(request2.session_request_id, signer=control_key)
    assert admissions.admit(request2, authorization2).state == "reserved"


def test_reconcile_cleans_snapshot_only_after_durable_admission_repair(tmp_path, monkeypatch):
    _, _, admissions, request, authorization, image, staged = _setup(tmp_path)
    executor, launches, _ = _launch_ambiguously(
        tmp_path, admissions, request, authorization, image, staged
    )
    execution_key = executor._execution_key(request, authorization.placement_generation)
    snapshot_root = executor._retained_snapshot_root(execution_key)
    assert snapshot_root.exists()

    original_mark_terminal = admissions.mark_terminal

    def fail_repair(*args, **kwargs):
        raise FleetPlacementError("simulated admission repair failure")

    monkeypatch.setattr(admissions, "mark_terminal", fail_repair)
    with pytest.raises(FleetPlacementError, match="simulated admission repair failure"):
        executor.reconcile(
            request.session_request_id,
            placement_generation=authorization.placement_generation,
        )
    assert snapshot_root.exists()

    monkeypatch.setattr(admissions, "mark_terminal", original_mark_terminal)
    assert executor.reconcile(
        request.session_request_id,
        placement_generation=authorization.placement_generation,
    ).state == "completed"
    assert not snapshot_root.exists()
    assert launches == ["app.exe"]


def test_reconciliation_intent_survives_crash_after_admission_terminalization(tmp_path, monkeypatch):
    _, _, admissions, request, authorization, image, staged = _setup(tmp_path)
    executor, launches, factory = _launch_ambiguously(
        tmp_path, admissions, request, authorization, image, staged
    )
    original_repair = executor._repair_admission

    def crash_after_repair(session_request_id, generation, state):
        effective = original_repair(session_request_id, generation, state)
        raise SystemExit(f"crash after admission became {effective}")

    monkeypatch.setattr(executor, "_repair_admission", crash_after_repair)
    with pytest.raises(SystemExit, match="crash after admission became completed"):
        executor.reconcile(
            request.session_request_id,
            placement_generation=authorization.placement_generation,
        )

    restarted = FleetNodeExecutor(
        tmp_path / "execution.sqlite3",
        admission_store=admissions,
        environment_factory=factory,
        image_path_resolver=lambda _request: image,
        staged_path_resolver=lambda _identity: staged,
        execution_probe=lambda _key: pytest.fail("durable reconciliation intent must replay before probing"),
    )
    recovered = restarted.reconcile(
        request.session_request_id,
        placement_generation=authorization.placement_generation,
    )
    assert recovered.state == "completed"
    assert launches == ["app.exe"]
