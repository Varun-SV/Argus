from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from pathlib import Path

import pytest

from argus.capsule.base import CapsuleError
from argus.capsule.control import (
    CapsuleControlRecord,
    CapsuleControlRegistry,
    CapsuleExecutionMode,
    CapsuleLifecycleState,
    GuestControlStateStore,
    new_capsule_id,
)


def _record(capsule_id: str) -> CapsuleControlRecord:
    return CapsuleControlRecord(
        capsule_id=capsule_id,
        provider="hyperv",
        provider_resource_identity="vm-guid:11111111-2222-3333-4444-555555555555",
        mutable_disk_identity="sha256:" + sha256(b"session-disk").hexdigest(),
        environment_id="env-sha256-" + "a" * 64,
        base_image_sha256="b" * 64,
        lifecycle_state=CapsuleLifecycleState.ALLOCATED.value,
        network_policy_identity="host_only:v1",
    )


def test_registry_reserves_monotonic_generations_and_allows_gaps(
    tmp_path: Path,
) -> None:
    registry = CapsuleControlRegistry(tmp_path / "control")
    capsule_id = new_capsule_id()
    registry.create(_record(capsule_id))

    _record1, generation1 = registry.reserve_generation(capsule_id)
    _record2, generation2 = registry.reserve_generation(capsule_id)
    assert (generation1, generation2) == (1, 2)

    committed = registry.commit_generation(
        capsule_id,
        generation2,
        execution_mode=CapsuleExecutionMode.ISOLATED.value,
    )
    assert committed.highest_reserved_generation == 2
    assert committed.last_committed_generation_known_by_host == 2

    with pytest.raises(CapsuleError, match="stale"):
        registry.commit_generation(
            capsule_id,
            generation1,
            execution_mode=CapsuleExecutionMode.ISOLATED.value,
        )

    _record3, generation3 = registry.reserve_generation(
        capsule_id,
        lifecycle_state=CapsuleLifecycleState.RECONNECTING.value,
    )
    assert generation3 == 3
    assert _record3.lifecycle_state == CapsuleLifecycleState.RECONNECTING.value


def test_registry_serializes_concurrent_generation_reservations(
    tmp_path: Path,
) -> None:
    root = tmp_path / "control"
    registry = CapsuleControlRegistry(root)
    capsule_id = new_capsule_id()
    registry.create(_record(capsule_id))

    def reserve(_: int) -> int:
        separate_process_view = CapsuleControlRegistry(root)
        return separate_process_view.reserve_generation(capsule_id)[1]

    with ThreadPoolExecutor(max_workers=8) as pool:
        generations = sorted(pool.map(reserve, range(16)))

    assert generations == list(range(1, 17))
    assert registry.load(capsule_id).highest_reserved_generation == 16


def test_guest_state_fences_stale_generation_and_capsule_identity(
    tmp_path: Path,
) -> None:
    store = GuestControlStateStore(tmp_path / "guest" / "control.json")
    capsule_id = new_capsule_id()
    first = store.commit_generation(
        capsule_id=capsule_id,
        generation=4,
        session_id="session-4",
        execution_mode="isolated",
    )
    assert first.highest_committed_generation == 4

    with pytest.raises(CapsuleError, match="stale"):
        store.commit_generation(
            capsule_id=capsule_id,
            generation=4,
            session_id="session-replay",
            execution_mode="isolated",
        )

    with pytest.raises(CapsuleError, match="contradicts"):
        store.commit_generation(
            capsule_id=new_capsule_id(),
            generation=5,
            session_id="session-5",
            execution_mode="isolated",
        )

    newer = store.commit_generation(
        capsule_id=capsule_id,
        generation=6,
        session_id="session-6",
        execution_mode="shared_user",
    )
    assert newer.highest_committed_generation == 6
    assert newer.last_execution_mode == "shared_user"


def test_control_records_contain_no_session_secrets(tmp_path: Path) -> None:
    registry = CapsuleControlRegistry(tmp_path / "control")
    capsule_id = new_capsule_id()
    registry.create(_record(capsule_id))
    registry.reserve_generation(capsule_id)

    raw = (tmp_path / "control" / f"{capsule_id}.json").read_text(
        encoding="utf-8"
    )
    for forbidden in (
        "bootstrap_token",
        "active_token",
        "private_key",
        "tls_private_key",
        "guest_token",
        "secret://",
    ):
        assert forbidden not in raw


def test_control_mode_and_generation_validation_fail_closed(tmp_path: Path) -> None:
    registry = CapsuleControlRegistry(tmp_path / "control")
    capsule_id = new_capsule_id()
    registry.create(_record(capsule_id))

    with pytest.raises(CapsuleError, match="unreserved"):
        registry.commit_generation(
            capsule_id,
            1,
            execution_mode="isolated",
        )

    _current, generation = registry.reserve_generation(capsule_id)
    with pytest.raises(CapsuleError, match="execution mode"):
        registry.commit_generation(
            capsule_id,
            generation,
            execution_mode="administrator",
        )
