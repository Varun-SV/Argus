from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import threading
import time

import pytest

from argus.capsule.base import (
    CapsuleHandle,
    CapsuleProvider,
    CapsuleProviderCapabilities,
    CapsuleRequest,
    CapsuleSettings,
    FailureCapsule,
)
from argus.capsule.control import CapsuleControlRegistry, CapsuleLifecycleState
from argus.execution.base import ExecutionEnvironmentError
from argus.execution.secure_capsule import SecureCapsuleExecutionEnvironment


_CURRENT_PROVIDER = [None]


class LifecycleProvider(CapsuleProvider):
    provider_name = "hyperv"
    provider_capabilities = CapsuleProviderCapabilities(
        provider="hyperv",
        host_platforms=("windows", "linux", "macos"),
        guest_os=("windows",),
        secure_transport=True,
        network_isolation=True,
        explicit_transfers=True,
        failure_retention=True,
        egress_allowlist=True,
    )

    def __init__(self, root: Path):
        self.root = root
        self.resource_id = "hyperv-uuid:12345678-1234-5678-1234-567812345678"
        self.disk_id = "file-v1:1:2:path-sha256-" + "d" * 64
        self.attached: list[Path] = []
        self.detached: list[Path] = []
        self.starts: list[CapsuleRequest] = []
        self.stops = 0
        self.destroyed = 0
        self.quarantined = 0
        self.block_reconnect_attach = False
        self.first_reconnect_attached = threading.Event()
        self.release_first_reconnect = threading.Event()
        self.reconnect_attach_entries: list[int] = []

    def create(self, request: CapsuleRequest) -> CapsuleHandle:
        raise AssertionError("provisioned Capsules must not use legacy create()")

    def create_stopped(self, request: CapsuleRequest) -> CapsuleHandle:
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "session.vhdx").write_bytes(b"mutable")
        return CapsuleHandle(
            session_id=request.session_id,
            provider=self.provider_name,
            vm_name="Argus-stable",
            root_dir=str(self.root),
            address="",
            guest_port=request.settings.guest_port,
            transport="https",
            guest_os="windows",
            architecture="x86_64",
            capsule_id=request.capsule_id,
            execution_mode=request.execution_mode,
            provider_resource_identity=self.resource_id,
            mutable_disk_identity=self.disk_id,
        )

    def attach_bootstrap(self, handle: CapsuleHandle, media: Path) -> None:
        assert media.is_file()
        self.attached.append(Path(media))
        if handle.control_generation >= 2:
            self.reconnect_attach_entries.append(handle.control_generation)
            if (
                self.block_reconnect_attach
                and len(self.reconnect_attach_entries) == 1
            ):
                self.first_reconnect_attached.set()
                if not self.release_first_reconnect.wait(timeout=3):
                    raise RuntimeError("concurrent reconnect test release timed out")

    def detach_bootstrap(self, handle: CapsuleHandle, media: Path) -> None:
        self.detached.append(Path(media))

    def start_existing(
        self,
        handle: CapsuleHandle,
        request: CapsuleRequest,
    ) -> CapsuleHandle:
        self.starts.append(request)
        return replace(
            handle,
            session_id=request.session_id,
            control_generation=request.control_generation,
            execution_mode=request.execution_mode,
            address="10.0.0.8",
        )

    def stop_existing(self, handle: CapsuleHandle) -> None:
        self.stops += 1

    def quarantine(self, handle: CapsuleHandle) -> None:
        self.quarantined += 1
        self.stop_existing(handle)

    def inspect_ownership(self, handle: CapsuleHandle) -> tuple[str, str]:
        return self.resource_id, self.disk_id

    def retain_failure(self, handle: CapsuleHandle, reason: str) -> FailureCapsule:
        return FailureCapsule(
            failure_id=handle.capsule_id,
            session_id=handle.session_id,
            provider=handle.provider,
            vm_name=handle.vm_name,
            root_dir=handle.root_dir,
            reason=reason,
            retained_at="2026-10-01T00:00:00+00:00",
            vm_state="Off",
            capsule_id=handle.capsule_id,
            failed_generation=handle.control_generation,
            execution_mode=handle.execution_mode,
            provider_resource_identity=handle.provider_resource_identity,
            mutable_disk_identity=handle.mutable_disk_identity,
        )

    def destroy(self, handle: CapsuleHandle) -> None:
        self.destroyed += 1


class LifecycleClient:
    fail_first_generation = False
    lose_after_guest_commit = False
    guest_highwater = 0
    created = []

    def __init__(self, endpoint, token, **kwargs):
        self.endpoint = endpoint
        self.token = token
        self.kwargs = kwargs
        self.request = None
        LifecycleClient.created.append(self)

    def wait_until_ready(self, timeout_seconds):
        provider = _CURRENT_PROVIDER[0]
        self.request = provider.starts[-1]
        if self.request.control_generation <= LifecycleClient.guest_highwater:
            raise RuntimeError("stale generation reached guest")
        if (
            LifecycleClient.fail_first_generation
            and self.request.control_generation == 1
        ):
            raise RuntimeError("generation-1 acknowledgement lost")

    def health(self):
        return {
            "ok": True,
            "secure": True,
            "capsule_id": self.request.capsule_id,
            "control_generation": self.request.control_generation,
            "execution_mode": self.request.execution_mode,
            "runtime_identity": self.request.settings.guest_runtime_identity,
        }

    def rotate_session_token(self, session_id, token, **kwargs):
        assert kwargs["capsule_id"] == self.request.capsule_id
        assert kwargs["control_generation"] == self.request.control_generation
        assert kwargs["execution_mode"] == self.request.execution_mode
        generation = self.request.control_generation
        LifecycleClient.guest_highwater = generation
        if (
            LifecycleClient.lose_after_guest_commit
            and generation == 1
        ):
            raise RuntimeError(
                "guest committed generation 1 but host lost acknowledgement"
            )
        self.token = token

    def close_session(self):
        return None


def _settings(tmp_path: Path, **overrides) -> CapsuleSettings:
    image = tmp_path / "golden.vhdx"
    image.write_bytes(b"immutable")
    values = dict(
        image=str(image),
        vm_root=str(tmp_path / "vm"),
        control_root=str(tmp_path / "control"),
        switch_name="Argus Internal",
        guest_transport="https",
        environment_id="env-sha256-" + "a" * 64,
        base_image_sha256="b" * 64,
        guest_runtime_identity="runtime-sha256-" + "c" * 64,
        retain_on_failure=True,
    )
    values.update(overrides)
    return CapsuleSettings(**values)


def _environment(tmp_path: Path, **overrides):
    provider = LifecycleProvider(tmp_path / "vm" / "stable")
    _CURRENT_PROVIDER[0] = provider
    LifecycleClient.created.clear()
    LifecycleClient.fail_first_generation = False
    LifecycleClient.lose_after_guest_commit = False
    LifecycleClient.guest_highwater = 0
    env = SecureCapsuleExecutionEnvironment(
        "cli",
        _settings(tmp_path, **overrides),
        provider=provider,
        client_factory=LifecycleClient,
        session_id="initial-session",
    )
    return env, provider


def test_provisioned_prepare_uses_reserved_generation_and_pinned_tls(
    tmp_path: Path,
) -> None:
    env, provider = _environment(tmp_path)
    env.prepare()

    assert provider.starts[0].control_generation == 1
    assert provider.attached and provider.detached
    assert provider.attached[0] == provider.detached[0]
    client = LifecycleClient.created[-1]
    assert client.kwargs["pinned_cert_sha256"]
    assert "ca_cert_path" not in client.kwargs
    record = CapsuleControlRegistry(
        env.settings.resolved_control_root
    ).load(env._capsule_id)
    assert record.highest_reserved_generation == 1
    assert record.last_committed_generation_known_by_host == 1
    assert record.lifecycle_state == CapsuleLifecycleState.ACTIVE.value
    assert record.provider_resource_identity == provider.resource_id
    assert record.mutable_disk_identity == provider.disk_id
    assert record.created_at and record.updated_at
    env.close()
    assert provider.destroyed == 1


def test_uncertain_first_generation_is_abandoned_not_reused(
    tmp_path: Path,
) -> None:
    env, provider = _environment(tmp_path)
    LifecycleClient.fail_first_generation = True
    env.prepare()
    assert [r.control_generation for r in provider.starts] == [1, 2]
    record = CapsuleControlRegistry(
        env.settings.resolved_control_root
    ).load(env._capsule_id)
    assert record.highest_reserved_generation == 2
    assert record.last_committed_generation_known_by_host == 2
    assert provider.stops >= 1
    env.close()


def test_guest_commit_with_lost_host_ack_advances_to_fresh_generation(
    tmp_path: Path,
) -> None:
    env, provider = _environment(tmp_path)
    LifecycleClient.lose_after_guest_commit = True

    env.prepare()

    assert [r.control_generation for r in provider.starts] == [1, 2]
    assert LifecycleClient.guest_highwater == 2
    record = CapsuleControlRegistry(
        env.settings.resolved_control_root
    ).load(env._capsule_id)
    assert record.highest_reserved_generation == 2
    assert record.last_committed_generation_known_by_host == 2
    assert provider.stops >= 1
    assert len(provider.attached) == 2
    assert len(provider.detached) == 2
    env.close()


def test_failure_reconnect_preserves_capsule_and_advances_generation(
    tmp_path: Path,
) -> None:
    env, provider = _environment(
        tmp_path,
        allowed_execution_modes=("isolated", "shared_user"),
    )
    env.prepare()
    capsule_id = env._capsule_id
    resource = env._handle.provider_resource_identity
    disk = env._handle.mutable_disk_identity
    first_session = env.session_id

    env.record_failure("application assertion")
    env.close()
    assert provider.quarantined == 1
    assert env.failure_capsule()["capsule_id"] == capsule_id

    env.reconnect_failure(execution_mode="shared_user")
    assert env._handle.capsule_id == capsule_id
    assert env._handle.provider_resource_identity == resource
    assert env._handle.mutable_disk_identity == disk
    assert env.session_id != first_session
    assert env._handle.control_generation == 2
    assert env._handle.execution_mode == "shared_user"
    env.close()


def test_concurrent_reconnects_serialize_provider_control_and_fence(
    tmp_path: Path,
) -> None:
    env, provider = _environment(tmp_path)
    env.prepare()
    capsule_id = env._capsule_id
    failure_generation = env._handle.control_generation
    env.record_failure("application assertion")
    env.close()
    retained = env._retained_failure
    assert retained is not None
    assert retained.failed_generation == failure_generation

    registry = CapsuleControlRegistry(env.settings.resolved_control_root)

    def reconnect_environment():
        candidate = SecureCapsuleExecutionEnvironment(
            "cli",
            env.settings,
            provider=provider,
            client_factory=LifecycleClient,
        )
        candidate._retained_failure = retained
        candidate._control_registry = registry
        candidate._capsule_id = capsule_id
        return candidate

    first = reconnect_environment()
    second = reconnect_environment()
    provider.block_reconnect_attach = True

    errors = []

    def run(candidate):
        try:
            candidate.reconnect_failure()
        except Exception as exc:
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(run, first)
        assert provider.first_reconnect_attached.wait(timeout=2)
        future_b = pool.submit(run, second)

        # B has started, but the per-Capsule operation lock must keep it out of
        # provider bootstrap control while A is paused inside that transaction.
        time.sleep(0.1)
        assert provider.reconnect_attach_entries == [2]

        provider.release_first_reconnect.set()
        future_a.result(timeout=3)
        future_b.result(timeout=3)

    assert not errors
    assert provider.reconnect_attach_entries == [2, 3]
    assert [request.control_generation for request in provider.starts] == [1, 2, 3]
    assert provider.starts[1].session_id != provider.starts[2].session_id
    record = registry.load(capsule_id)
    assert record.highest_reserved_generation == 3
    assert record.last_committed_generation_known_by_host == 3
    assert record.lifecycle_state == CapsuleLifecycleState.ACTIVE.value

    # The later reconnect is authoritative. Whichever object owns generation 3
    # may clean up the fake provider state; generation 2 is deliberately stale.
    winner = first if first._handle.control_generation == 3 else second
    winner.close()


def test_llm_cannot_expand_execution_mode_policy(tmp_path: Path) -> None:
    env, _provider = _environment(
        tmp_path,
        allowed_execution_modes=("isolated", "shared_user"),
        allow_llm_mode_change=False,
    )
    env.prepare()
    with pytest.raises(
        ExecutionEnvironmentError,
        match="disabled by user policy",
    ):
        env.transition_execution_mode(
            "shared_user",
            requested_by_llm=True,
        )
    assert env._handle.control_generation == 1
    env.close()


def test_static_token_is_rejected_for_provisioned_capsule(tmp_path: Path) -> None:
    env, provider = _environment(tmp_path, guest_token="legacy-static")
    with pytest.raises(ExecutionEnvironmentError, match="static guest_token"):
        env.prepare()
    assert provider.destroyed == 0


def test_generation_secrets_never_enter_durable_capsule_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argus.capsule.bootstrap as bootstrap_module
    import argus.execution.secure_capsule as secure_module

    bootstrap_sentinel = "ARGUS_BOOTSTRAP_SECRET_SENTINEL_" + "B" * 48
    active_sentinel = "ARGUS_ACTIVE_SECRET_SENTINEL_" + "A" * 48

    monkeypatch.setattr(
        bootstrap_module.secrets,
        "token_urlsafe",
        lambda _size: bootstrap_sentinel,
    )
    monkeypatch.setattr(
        secure_module.secrets,
        "token_urlsafe",
        lambda _size: active_sentinel,
    )

    env, _provider = _environment(tmp_path)
    env.prepare()
    env.record_failure("sentinel failure")
    env.close()

    failure_payload = repr(env.failure_capsule()).encode("utf-8")
    assert bootstrap_sentinel.encode() not in failure_payload
    assert active_sentinel.encode() not in failure_payload

    control_root = env.settings.resolved_control_root
    for artifact in control_root.rglob("*"):
        if not artifact.is_file() or artifact.is_symlink():
            continue
        data = artifact.read_bytes()
        assert bootstrap_sentinel.encode() not in data, artifact
        assert active_sentinel.encode() not in data, artifact

    # Successful establishment must also have deleted one-attempt media and
    # host-side plaintext token/key staging.
    assert not list(
        (control_root / "bootstrap-attempts").glob("*")
    )
    assert not list(
        (control_root / "bootstrap-media").glob("*.iso")
    )


def test_reconnect_refuses_lost_host_record(
    tmp_path: Path,
) -> None:
    env, provider = _environment(tmp_path)
    env.prepare()
    capsule_id = env._capsule_id
    env.record_failure("application assertion")
    env.close()
    record_path = (
        env.settings.resolved_control_root / f"{capsule_id}.json"
    )
    record_path.unlink()
    with pytest.raises(Exception, match="control record is missing"):
        env.reconnect_failure()
    assert provider.destroyed == 0


def test_reconnect_refuses_provider_identity_substitution(
    tmp_path: Path,
) -> None:
    env, provider = _environment(tmp_path)
    env.prepare()
    env.record_failure("application assertion")
    env.close()
    provider.resource_id = (
        "hyperv-uuid:aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    )
    with pytest.raises(
        ExecutionEnvironmentError,
        match="ownership no longer matches",
    ):
        env.reconnect_failure()
