from __future__ import annotations

import pytest

import argus.ates.artifacts as artifacts_module
from argus.ates import (
    ArtifactCaptureConfig,
    ArtifactCaptureError,
    ArtifactCapturePolicy,
    ArtifactContext,
    AtesArtifactRepository,
    AtesEventStore,
    RunId,
)
from argus.engine.ates_artifacts import RuntimeArtifactCapture
from argus.engine.ates_runtime import AtesRuntimeError, AtesRuntimeRecorder
from argus.engine.spec import parse_spec
from tests.conftest import FakeAdapter, FakeProvider


def _payload_files(store):
    root = store.run_dir / "artifacts"
    if not root.exists():
        return []
    return sorted(path for path in root.rglob("*") if path.is_file())


def test_directory_durability_failure_rolls_back_published_artifact(tmp_path, monkeypatch):
    store = AtesEventStore(tmp_path, RunId.new())
    repository = AtesArtifactRepository(store)
    real_fsync = artifacts_module._PinnedDirectory.fsync
    failed = False

    def fail_after_publish(self):
        nonlocal failed
        has_png = self.path.exists() and any(self.path.glob("*.png"))
        if has_png and not failed:
            failed = True
            raise OSError("simulated artifact directory fsync failure")
        return real_fsync(self)

    monkeypatch.setattr(artifacts_module._PinnedDirectory, "fsync", fail_after_publish)
    try:
        with pytest.raises(
            ArtifactCaptureError,
            match="durability/authority barrier failed and was rolled back",
        ):
            repository.capture_bytes(
                b"private-payload",
                context=ArtifactContext.FAILURE_SCREENSHOT,
                kind="screenshot",
                media_type="image/png",
            )
        assert failed is True
        assert _payload_files(store) == []
    finally:
        store.close()


def test_hmac_key_generation_failure_removes_partial_key_and_unregistered_payload(
    tmp_path, monkeypatch
):
    store = AtesEventStore(tmp_path, RunId.new())
    repository = AtesArtifactRepository(store)

    def fail_key_generation(_size):
        raise RuntimeError("simulated key generation failure")

    monkeypatch.setattr(artifacts_module.secrets, "token_bytes", fail_key_generation)
    try:
        with pytest.raises(RuntimeError, match="key generation failure"):
            repository.capture_bytes(
                b"protected-private-payload",
                context=ArtifactContext.FAILURE_SCREENSHOT,
                kind="screenshot",
                media_type="image/png",
            )
        assert not (store.run_dir / ".ates-artifact-hmac-key").exists()
        assert _payload_files(store) == []
    finally:
        store.close()


def test_integrated_runtime_rejects_unbound_custom_artifact_policy(tmp_path):
    spec = parse_spec(
        """\
name: runtime artifact policy provenance
target: {adapter: desktop-gui, launch: fake.exe}
steps:
  - "done"
"""
    )
    recorder = AtesRuntimeRecorder.for_scripted(
        tmp_path,
        spec,
        FakeProvider([]),
        FakeAdapter(),
    )
    try:
        custom = ArtifactCapturePolicy(
            ArtifactCaptureConfig(
                suppressed_contexts=frozenset({ArtifactContext.FAILURE_SCREENSHOT})
            )
        )
        with pytest.raises(AtesRuntimeError, match="not provenance-bound"):
            RuntimeArtifactCapture(recorder, custom)
    finally:
        recorder.close()
