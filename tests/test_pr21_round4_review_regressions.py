from __future__ import annotations

import hashlib
import os
from types import SimpleNamespace

import pytest

import argus.ates.artifacts as artifacts_module
import argus.execution.ates_collection as collection_module
from argus.ates import ArtifactContext, AtesArtifactRepository, AtesEventStore, AtesStoreError, RunId
from argus.ates.artifacts import ArtifactPublicationError
from argus.execution.base import ExecutionEnvironmentError


class _InfoClient:
    def collect_info(self, _path):
        data = b"x"
        return {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}

    def _request(self, _method, _endpoint):  # pragma: no cover
        raise AssertionError("streaming is replaced by the regression stub")


def _artifact_payloads(project_dir):
    root = project_dir / ".argus" / "runs"
    if not root.exists():
        return []
    payloads = []
    for artifacts_dir in root.glob("RUN-*/artifacts"):
        payloads.extend(path for path in artifacts_dir.rglob("*") if path.is_file())
    return payloads


def test_mapped_collection_no_overwrite_conflict_preserves_existing_final(
    tmp_path, monkeypatch
):
    store = AtesEventStore(tmp_path, RunId.new())
    repository = AtesArtifactRepository(store)
    reservation = repository.reserve_protected_collection(1)[0]
    relative = reservation.relative_path
    existing = b"previously retained evidence"
    replacement = b"new transaction bytes"
    environment = SimpleNamespace(_workspace_ready=True, _client=_InfoClient())

    def attempt_real_no_overwrite(
        _client,
        _guest,
        output_tree,
        *,
        info,
        destination_relative,
    ):
        _ = info
        handle, temp_name, _destination = output_tree.open_temp_file(destination_relative)
        try:
            with handle:
                handle.write(replacement)
                handle.flush()
                os.fsync(handle.fileno())
            output_tree.commit_temp(destination_relative, temp_name)
        except Exception:
            output_tree.remove_temp(destination_relative, temp_name)
            raise
        raise AssertionError("no-overwrite conflict unexpectedly published replacement bytes")

    monkeypatch.setattr(
        collection_module,
        "collect_file_to_pinned_tree",
        attempt_real_no_overwrite,
    )

    try:
        with repository.open_tree((relative,)) as tree:
            handle, temp_name, destination = tree.open_temp_file(relative)
            with handle:
                handle.write(existing)
                handle.flush()
                os.fsync(handle.fileno())
            tree.commit_temp(relative, temp_name)
            assert destination.read_bytes() == existing

            with pytest.raises(
                ExecutionEnvironmentError,
                match="protected Capsule artifact collection failed",
            ):
                collection_module.collect_capsule_artifacts_to_tree(
                    environment,
                    ({"path": "secret.bin", "destination": relative},),
                    tree,
                )

            assert destination.read_bytes() == existing
    finally:
        store.close()


def test_capture_retries_final_cleanup_after_internal_rollback_becomes_ambiguous(
    tmp_path, monkeypatch
):
    real_commit = artifacts_module._AtesArtifactTree.commit_temp
    real_rollback = artifacts_module._AtesArtifactTree._rollback_published
    real_ensure_absent = artifacts_module._AtesArtifactTree.ensure_relative_absent
    state = {"internal_rollback_calls": 0, "caller_retry_calls": 0, "barrier_failures": 0}

    def fail_first_internal_rollback(parent, final_name):
        state["internal_rollback_calls"] += 1
        if state["internal_rollback_calls"] == 1:
            raise OSError("forced first internal rollback failure")
        return real_rollback(parent, final_name)

    def force_post_publication_barrier_failure(self, relative, temp_name):
        parent, _final_name, _normalized = self._parent(relative)
        real_fsync = parent.fsync
        failed = False

        def fail_once():
            nonlocal failed
            if not failed:
                failed = True
                state["barrier_failures"] += 1
                raise OSError("forced post-publication durability failure")
            return real_fsync()

        parent.fsync = fail_once
        try:
            return real_commit(self, relative, temp_name)
        finally:
            parent.fsync = real_fsync

    def track_caller_retry(self, relative):
        state["caller_retry_calls"] += 1
        return real_ensure_absent(self, relative)

    monkeypatch.setattr(
        artifacts_module._AtesArtifactTree,
        "_rollback_published",
        staticmethod(fail_first_internal_rollback),
    )
    monkeypatch.setattr(
        artifacts_module._AtesArtifactTree,
        "commit_temp",
        force_post_publication_barrier_failure,
    )
    monkeypatch.setattr(
        artifacts_module._AtesArtifactTree,
        "ensure_relative_absent",
        track_caller_retry,
    )

    store = AtesEventStore(tmp_path, RunId.new())
    repository = AtesArtifactRepository(store)
    try:
        with pytest.raises(ArtifactPublicationError) as raised:
            repository.capture_bytes(
                b"private screenshot",
                context=ArtifactContext.FAILURE_SCREENSHOT,
                kind="screenshot",
                media_type="image/png",
            )
    finally:
        store.close()

    assert raised.value.final_may_exist is True
    assert state["barrier_failures"] == 1
    assert state["internal_rollback_calls"] == 1
    assert state["caller_retry_calls"] == 1
    assert _artifact_payloads(tmp_path) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX pinned-directory identity regression")
def test_close_authority_loss_rolls_back_through_original_pinned_directory(
    tmp_path, monkeypatch
):
    real_close = artifacts_module._AtesArtifactTree.close
    state = {}

    def replace_namespace_before_close(self, *, suppress_errors=False):
        if (
            not suppress_errors
            and self._close_rollback_relatives
            and "moved_dir" not in state
        ):
            relative = self._close_rollback_relatives[0]
            parent, final_name, _normalized = self._parent(relative)
            original_dir = parent.path
            moved_dir = original_dir.with_name(original_dir.name + "-moved-by-review-test")
            os.rename(original_dir, moved_dir)
            original_dir.mkdir()
            state.update(
                original_dir=original_dir,
                moved_dir=moved_dir,
                final_name=final_name,
            )
        return real_close(self, suppress_errors=suppress_errors)

    monkeypatch.setattr(
        artifacts_module._AtesArtifactTree,
        "close",
        replace_namespace_before_close,
    )

    store = AtesEventStore(tmp_path, RunId.new())
    repository = AtesArtifactRepository(store)
    try:
        with pytest.raises(AtesStoreError, match="namespace no longer refers"):
            repository.capture_bytes(
                b"private screenshot",
                context=ArtifactContext.FAILURE_SCREENSHOT,
                kind="screenshot",
                media_type="image/png",
            )
    finally:
        store.close()

    moved_final = state["moved_dir"] / state["final_name"]
    replacement_final = state["original_dir"] / state["final_name"]
    assert not moved_final.exists()
    assert not replacement_final.exists()
