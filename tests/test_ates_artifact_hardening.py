from __future__ import annotations

import os
from pathlib import Path

import pytest

import argus.ates.artifacts as artifacts_module
from argus.ates import (
    ArtifactCaptureError,
    ArtifactContext,
    ArtifactId,
    AtesArtifactRepository,
    AtesEventStore,
    RunId,
)


def _repo(tmp_path):
    store = AtesEventStore(tmp_path, RunId.new())
    return store, AtesArtifactRepository(store)


def _payload_files(store):
    root = store.run_dir / "artifacts"
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def test_existing_final_name_is_never_overwritten(tmp_path, monkeypatch):
    fixed = ArtifactId("ART-collision-test")
    monkeypatch.setattr(artifacts_module.ArtifactId, "new", lambda: fixed)
    store, repository = _repo(tmp_path)
    relative = f"protected/screenshot/{fixed}.png"
    try:
        with repository.open_tree((relative,)) as tree:
            destination = tree.lexical_path(relative)
            destination.write_bytes(b"preexisting")

        with pytest.raises(ArtifactCaptureError, match="artifact commit failed"):
            repository.capture_bytes(
                b"new-private-payload",
                context=ArtifactContext.FAILURE_SCREENSHOT,
                kind="screenshot",
                media_type="image/png",
            )

        destination = store.run_dir / "artifacts" / relative
        assert destination.read_bytes() == b"preexisting"
        assert b"new-private-payload" not in destination.read_bytes()
    finally:
        store.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX hardlink publication regression")
def test_link_then_temp_unlink_failure_rolls_back_final_link(tmp_path, monkeypatch):
    store, repository = _repo(tmp_path)
    real_unlink = artifacts_module.os.unlink
    failed_once = False

    def flaky_unlink(path, *args, **kwargs):
        nonlocal failed_once
        text = os.fspath(path)
        if not failed_once and text.endswith(".part"):
            failed_once = True
            raise OSError("simulated temp unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(artifacts_module.os, "unlink", flaky_unlink)
    try:
        with pytest.raises(ArtifactCaptureError, match="artifact commit failed"):
            repository.capture_bytes(
                b"private-payload",
                context=ArtifactContext.FAILURE_SCREENSHOT,
                kind="screenshot",
                media_type="image/png",
            )
        assert failed_once is True
        assert _payload_files(store) == []
    finally:
        store.close()


def test_post_publication_verification_failure_removes_unregistered_file(tmp_path, monkeypatch):
    store, repository = _repo(tmp_path)

    def fail_digest(self, relative):
        raise ArtifactCaptureError("simulated post-publication verification failure")

    monkeypatch.setattr(artifacts_module._AtesArtifactTree, "digest_existing", fail_digest)
    try:
        with pytest.raises(ArtifactCaptureError, match="post-publication verification failure"):
            repository.capture_bytes(
                b"private-payload",
                context=ArtifactContext.FAILURE_SCREENSHOT,
                kind="screenshot",
                media_type="image/png",
            )
        assert _payload_files(store) == []
    finally:
        store.close()
