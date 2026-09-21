from __future__ import annotations

import hashlib

import pytest

from argus.adapters.base import Observation
from argus.ates import RunId
from argus.execution.base import ExecutionEnvironment
from argus.fleet.enrollment import EnrollmentAcknowledgement, EnrollmentRequest, FleetEnrollmentRegistry
from argus.fleet.execution import FleetNodeExecutor, VerifiedLaunchInputs
from argus.fleet.identity import ControlCenterKeyPair, NodeKeyPair
from argus.fleet.placement import (
    FleetPlacementError,
    FleetPlacementStore,
    NodeAdmissionStore,
    SessionRequest,
    StagedInputIdentity,
)


class _FakeCapsuleEnvironment(ExecutionEnvironment):
    environment_type = "capsule"
    isolated = True
    location = "test-capsule"
    type_name = "test"

    def __init__(self, launches, *, execution_key=None, launched_keys=None):
        self.launches = launches
        self.execution_key = execution_key
        self.launched_keys = launched_keys

    def launch(self, target: str) -> None:
        if self.execution_key is not None and self.launched_keys is not None:
            if self.execution_key in self.launched_keys:
                return
            self.launched_keys.add(self.execution_key)
        self.launches.append(target)

    def observe(self, include_screenshot: bool = True) -> Observation:
        return Observation(window_title="fake")

    def act(self, action: dict) -> str:
        return "ok"

    def close(self) -> None:
        pass


class _LostResponseCapsuleEnvironment(_FakeCapsuleEnvironment):
    def launch(self, target: str) -> None:
        already_launched = self.execution_key in self.launched_keys
        super().launch(target)
        if not already_launched:
            raise RuntimeError("launch response lost")


class _SnapshotCapsuleEnvironment(_FakeCapsuleEnvironment):
    def __init__(self, launches, verified: VerifiedLaunchInputs, **kwargs):
        super().__init__(launches, **kwargs)
        self.verified = verified

    def launch(self, target: str) -> None:
        assert self.verified.image_path.read_bytes() == b"immutable capsule image"
        assert self.verified.staged_paths["input.bin"].read_bytes() == b"immutable staged input"
        super().launch(target)


class _FakeLocalEnvironment(_FakeCapsuleEnvironment):
    environment_type = "local"


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _setup(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
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
        EnrollmentAcknowledgement.create(result=pending, key_pair=node_key),
        now=102,
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
    return node, control_key, placements, admissions


def _request(tmp_path):
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
    return request, image, staged


def test_dispatch_is_capsule_only_idempotent_and_restart_reconcilable(tmp_path):
    node, control_key, placements, admissions = _setup(tmp_path)
    request, image, staged = _request(tmp_path)
    placements.create_placement(request, owner_node_id=node.node_id, now=1000)
    authorization = placements.authorization_for_dispatch(request.session_request_id, signer=control_key)
    launches = []
    launched_keys = set()
    factory_calls = []

    def factory(_request, _verified, execution_key):
        factory_calls.append(1)
        return _FakeCapsuleEnvironment(launches, execution_key=execution_key, launched_keys=launched_keys)

    kwargs = dict(
        admission_store=admissions,
        environment_factory=factory,
        image_path_resolver=lambda _request: image,
        staged_path_resolver=lambda _identity: staged,
        execution_probe=lambda _key: "running",
    )
    executor = FleetNodeExecutor(tmp_path / "execution.sqlite3", **kwargs)
    first = executor.dispatch(request, authorization, target="app.exe")
    assert first.state == "running"
    assert launches == ["app.exe"]
    assert len(factory_calls) == 1

    replay = executor.dispatch(request, authorization, target="app.exe")
    assert replay.state == "running"
    assert launches == ["app.exe"]
    assert len(factory_calls) == 1

    restarted = FleetNodeExecutor(tmp_path / "execution.sqlite3", **kwargs)
    assert restarted.reconcile(
        request.session_request_id,
        placement_generation=authorization.placement_generation,
    ).state == "running"
    assert restarted.dispatch(request, authorization, target="app.exe").state == "running"
    assert launches == ["app.exe"]
    assert len(factory_calls) == 1


def test_launch_side_effect_then_lost_response_stays_reconcilable(tmp_path):
    node, control_key, placements, admissions = _setup(tmp_path)
    request, image, staged = _request(tmp_path)
    placements.create_placement(request, owner_node_id=node.node_id, now=1000)
    authorization = placements.authorization_for_dispatch(request.session_request_id, signer=control_key)
    launches = []
    launched_keys = set()
    factory_calls = []

    def factory(_request, _verified, execution_key):
        factory_calls.append(1)
        return _LostResponseCapsuleEnvironment(
            launches, execution_key=execution_key, launched_keys=launched_keys
        )

    kwargs = dict(
        admission_store=admissions,
        environment_factory=factory,
        image_path_resolver=lambda _request: image,
        staged_path_resolver=lambda _identity: staged,
        execution_probe=lambda _key: "running",
    )
    executor = FleetNodeExecutor(tmp_path / "execution.sqlite3", **kwargs)
    with pytest.raises(RuntimeError, match="launch response lost"):
        executor.dispatch(request, authorization, target="app.exe")
    assert launches == ["app.exe"]
    assert len(factory_calls) == 1

    # Mutable staging is no longer required once launch has crossed the
    # ambiguous side-effect boundary; recovery uses the retained verified copy.
    image.unlink()
    staged.unlink()

    restarted = FleetNodeExecutor(tmp_path / "execution.sqlite3", **kwargs)
    assert restarted.dispatch(request, authorization, target="app.exe").state == "running"
    assert launches == ["app.exe"]
    assert len(factory_calls) == 2

    assert restarted.reconcile(
        request.session_request_id,
        placement_generation=authorization.placement_generation,
    ).state == "running"
    assert launches == ["app.exe"]


def test_crash_after_durable_claim_before_launch_is_recoverable(tmp_path):
    node, control_key, placements, admissions = _setup(tmp_path)
    request, image, staged = _request(tmp_path)
    placements.create_placement(request, owner_node_id=node.node_id, now=1000)
    authorization = placements.authorization_for_dispatch(request.session_request_id, signer=control_key)
    launches = []
    launched_keys = set()

    def crash_before_environment(_request, _verified, _execution_key):
        raise SystemExit("node process died after durable claim")

    executor = FleetNodeExecutor(
        tmp_path / "execution.sqlite3",
        admission_store=admissions,
        environment_factory=crash_before_environment,
        image_path_resolver=lambda _request: image,
        staged_path_resolver=lambda _identity: staged,
    )
    with pytest.raises(SystemExit, match="node process died"):
        executor.dispatch(request, authorization, target="app.exe")
    assert launches == []

    def recovery_factory(_request, _verified, execution_key):
        return _FakeCapsuleEnvironment(
            launches, execution_key=execution_key, launched_keys=launched_keys
        )

    restarted = FleetNodeExecutor(
        tmp_path / "execution.sqlite3",
        admission_store=admissions,
        environment_factory=recovery_factory,
        image_path_resolver=lambda _request: image,
        staged_path_resolver=lambda _identity: staged,
    )
    recovered = restarted.dispatch(request, authorization, target="app.exe")
    assert recovered.state == "running"
    assert launches == ["app.exe"]

    assert restarted.dispatch(request, authorization, target="app.exe").state == "running"
    assert launches == ["app.exe"]


def test_dispatch_verifies_bytes_before_launch_releases_capacity_and_never_falls_back_local(tmp_path):
    node, control_key, placements, admissions = _setup(tmp_path)
    request, image, staged = _request(tmp_path)
    placements.create_placement(request, owner_node_id=node.node_id, now=1000)
    authorization = placements.authorization_for_dispatch(request.session_request_id, signer=control_key)

    staged.write_bytes(b"tampered")
    called = []
    executor = FleetNodeExecutor(
        tmp_path / "execution.sqlite3",
        admission_store=admissions,
        environment_factory=lambda _request, _verified, _key: called.append(1) or _FakeCapsuleEnvironment([]),
        image_path_resolver=lambda _request: image,
        staged_path_resolver=lambda _identity: staged,
    )
    with pytest.raises(FleetPlacementError, match="staged input bytes"):
        executor.dispatch(request, authorization, target="app.exe")
    assert called == []

    request2, image2, staged2 = _request(tmp_path)
    placements.create_placement(request2, owner_node_id=node.node_id, now=1001)
    authorization2 = placements.authorization_for_dispatch(request2.session_request_id, signer=control_key)
    launches = []
    executor2 = FleetNodeExecutor(
        tmp_path / "execution-2.sqlite3",
        admission_store=admissions,
        environment_factory=lambda _request, _verified, _key: _FakeCapsuleEnvironment(launches),
        image_path_resolver=lambda _request: image2,
        staged_path_resolver=lambda _identity: staged2,
    )
    assert executor2.dispatch(request2, authorization2, target="app.exe").state == "running"
    assert launches == ["app.exe"]

    local_root = tmp_path / "local-case"
    node3, control_key3, placements3, admissions3 = _setup(local_root)
    request3, image3, staged3 = _request(local_root)
    placements3.create_placement(request3, owner_node_id=node3.node_id, now=1000)
    authorization3 = placements3.authorization_for_dispatch(request3.session_request_id, signer=control_key3)
    local_launches = []
    executor3 = FleetNodeExecutor(
        local_root / "execution.sqlite3",
        admission_store=admissions3,
        environment_factory=lambda _request, _verified, _key: _FakeLocalEnvironment(local_launches),
        image_path_resolver=lambda _request: image3,
        staged_path_resolver=lambda _identity: staged3,
    )
    with pytest.raises(FleetPlacementError, match="local fallback is forbidden"):
        executor3.dispatch(request3, authorization3, target="app.exe")
    assert local_launches == []


def test_launch_consumes_verified_snapshot_not_replaced_source(tmp_path):
    node, control_key, placements, admissions = _setup(tmp_path)
    request, image, staged = _request(tmp_path)
    placements.create_placement(request, owner_node_id=node.node_id, now=1000)
    authorization = placements.authorization_for_dispatch(request.session_request_id, signer=control_key)
    launches = []

    def factory(_request, verified, _execution_key):
        image.write_bytes(b"replacement image")
        staged.write_bytes(b"replacement input")
        return _SnapshotCapsuleEnvironment(launches, verified)

    executor = FleetNodeExecutor(
        tmp_path / "execution.sqlite3",
        admission_store=admissions,
        environment_factory=factory,
        image_path_resolver=lambda _request: image,
        staged_path_resolver=lambda _identity: staged,
    )
    assert executor.dispatch(request, authorization, target="app.exe").state == "running"
    assert launches == ["app.exe"]
    assert image.read_bytes() == b"replacement image"
    assert staged.read_bytes() == b"replacement input"
