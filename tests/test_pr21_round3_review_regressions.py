from __future__ import annotations

import hashlib
import json
import os
from types import SimpleNamespace

import pytest

import argus.ates.artifacts as artifacts_module
import argus.execution.ates_collection as collection_module
from argus.ates import (
    ArtifactCaptureError,
    ArtifactContext,
    AtesArtifactRepository,
    AtesEventStore,
    EventType,
    RunId,
)
from argus.ates.artifacts import ArtifactPublicationError
from argus.engine.ates_artifacts import RuntimeArtifactCapture
from argus.engine.ates_runtime import AtesRuntimeRecorder
from argus.engine.runner import run_test
from argus.engine.spec import parse_spec
from argus.execution.base import ExecutionEnvironmentError
from tests.conftest import FakeAdapter, FakeProvider


def _artifact_payloads(project_dir):
    root = project_dir / ".argus" / "runs"
    if not root.exists():
        return []
    payloads = []
    for artifacts_dir in root.glob("RUN-*/artifacts"):
        payloads.extend(path for path in artifacts_dir.rglob("*") if path.is_file())
    return payloads


def _fail_first_clean_tree_close(monkeypatch):
    real_assert = artifacts_module._AtesArtifactTree.assert_authoritative
    state = {"failed": False}

    def fail_when_close_rollback_is_armed(self):
        if self._close_rollback_relatives and not state["failed"]:
            state["failed"] = True
            raise ArtifactCaptureError("forced final artifact-tree close failure")
        return real_assert(self)

    monkeypatch.setattr(
        artifacts_module._AtesArtifactTree,
        "assert_authoritative",
        fail_when_close_rollback_is_armed,
    )
    return state


def test_screenshot_close_failure_rolls_back_pre_event_payload(tmp_path, monkeypatch):
    store = AtesEventStore(tmp_path, RunId.new())
    repository = AtesArtifactRepository(store)
    state = _fail_first_clean_tree_close(monkeypatch)

    try:
        with pytest.raises(ArtifactCaptureError, match="forced final artifact-tree close failure"):
            repository.capture_bytes(
                b"private screenshot",
                context=ArtifactContext.FAILURE_SCREENSHOT,
                kind="screenshot",
                media_type="image/png",
            )
    finally:
        store.close()

    assert state["failed"] is True
    assert _artifact_payloads(tmp_path) == []


class _TreeWritingCollectionAdapter(FakeAdapter):
    def collect_artifacts_to_tree(self, entries, output_tree):
        collected = []
        for entry in entries:
            data = b"private collected bytes"
            relative = entry["destination"]
            handle, temp_name, _destination = output_tree.open_temp_file(relative)
            with handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            output_tree.commit_temp(relative, temp_name)
            collected.append(
                {
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        return collected


def test_collection_close_failure_rolls_back_before_artifact_events(tmp_path, monkeypatch):
    spec = parse_spec(
        """\
name: collection close transaction
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "finish"
collect:
  - secret-output.bin
"""
    )
    provider = FakeProvider([])
    adapter = _TreeWritingCollectionAdapter()
    recorder = AtesRuntimeRecorder.for_scripted(tmp_path, spec, provider, adapter)
    capture = RuntimeArtifactCapture(recorder)
    run_id = recorder.run_id
    state = _fail_first_clean_tree_close(monkeypatch)

    try:
        with pytest.raises(ArtifactCaptureError, match="forced final artifact-tree close failure"):
            capture.collect_declared(adapter, ("secret-output.bin",))
    finally:
        recorder.close()

    assert state["failed"] is True
    assert _artifact_payloads(tmp_path) == []
    with AtesEventStore(tmp_path, run_id) as store:
        assert all(
            event.envelope.event_type is not EventType.ARTIFACT_COLLECTED
            for event in store.events
        )


class _InfoClient:
    def collect_info(self, _path):
        data = b"x"
        return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def _request(self, _method, _endpoint):  # pragma: no cover
        raise AssertionError("streaming is replaced by the regression stub")


class _RollbackTree:
    def __init__(self, *, fail_rollback=False):
        self.published = set()
        self.unlinked = []
        self.fail_rollback = fail_rollback

    def ensure_relative_absent(self, relative):
        self.unlinked.append(relative)
        if self.fail_rollback:
            raise OSError("forced durable rollback failure")
        self.published.discard(relative)

    def unlink_relative(self, relative):
        self.ensure_relative_absent(relative)


def test_collection_retries_rollback_for_current_ambiguous_destination(monkeypatch):
    destination = "protected/collected_file/current.bin"
    tree = _RollbackTree()
    environment = SimpleNamespace(_workspace_ready=True, _client=_InfoClient())

    def publish_then_fail(_client, _guest, output_tree, *, info, destination_relative):
        _ = info
        output_tree.published.add(destination_relative)
        raise ArtifactPublicationError(
            "forced ambiguous publication",
            final_may_exist=True,
        )

    monkeypatch.setattr(
        collection_module,
        "collect_file_to_pinned_tree",
        publish_then_fail,
    )

    with pytest.raises(ExecutionEnvironmentError, match="protected Capsule artifact collection failed"):
        collection_module.collect_capsule_artifacts_to_tree(
            environment,
            ({"path": "secret.bin", "destination": destination},),
            tree,
        )

    assert tree.unlinked == [destination]
    assert tree.published == set()


def test_collection_escalates_when_current_destination_rollback_is_ambiguous(monkeypatch):
    destination = "protected/collected_file/current.bin"
    tree = _RollbackTree(fail_rollback=True)
    environment = SimpleNamespace(_workspace_ready=True, _client=_InfoClient())

    def publish_then_fail(_client, _guest, output_tree, *, info, destination_relative):
        _ = info
        output_tree.published.add(destination_relative)
        raise ArtifactPublicationError(
            "forced ambiguous publication",
            final_may_exist=True,
        )

    monkeypatch.setattr(
        collection_module,
        "collect_file_to_pinned_tree",
        publish_then_fail,
    )

    with pytest.raises(ExecutionEnvironmentError, match="incomplete or ambiguous"):
        collection_module.collect_capsule_artifacts_to_tree(
            environment,
            ({"path": "secret.bin", "destination": destination},),
            tree,
        )

    assert tree.unlinked == [destination]


class _TransferPreparedAdapter(FakeAdapter):
    def prepare_transfers(self):
        return None


def test_on_step_collect_mutation_cannot_change_committed_collection_order(
    tmp_path, monkeypatch
):
    spec = parse_spec(
        """\
name: immutable collection declaration
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "finish"
collect:
  - original-output.bin
"""
    )
    provider = FakeProvider([json.dumps({"action": "done", "success": True})])
    adapter = _TransferPreparedAdapter()
    committed_collect = []
    executed_collect = []

    original_for_scripted = AtesRuntimeRecorder.for_scripted.__func__

    def recording_for_scripted(
        cls,
        project_dir,
        committed_spec,
        provider_arg,
        adapter_arg,
        *,
        privacy_policy=None,
    ):
        committed_collect.append(tuple(committed_spec.collect))
        return original_for_scripted(
            cls,
            project_dir,
            committed_spec,
            provider_arg,
            adapter_arg,
            privacy_policy=privacy_policy,
        )

    def recording_collect(self, _adapter, guest_paths):
        executed_collect.append(tuple(guest_paths))
        return []

    monkeypatch.setattr(
        AtesRuntimeRecorder,
        "for_scripted",
        classmethod(recording_for_scripted),
    )
    monkeypatch.setattr(RuntimeArtifactCapture, "collect_declared", recording_collect)

    def mutate_after_step(_step):
        spec.collect[:] = ["replacement-output.bin"]

    result = run_test(
        spec,
        provider,
        adapter,
        project_dir=tmp_path,
        on_step=mutate_after_step,
    )

    assert result.status == "pass"
    assert spec.collect == ["replacement-output.bin"]
    assert committed_collect == [("original-output.bin",)]
    assert executed_collect == [("original-output.bin",)]
