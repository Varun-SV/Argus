from __future__ import annotations

import json
from pathlib import Path

import pytest

from argus.capsule.bootstrap import create_bootstrap_attempt
from argus.capsule.bootstrap_service import prepare_bootstrap_service
from argus.capsule.control import new_capsule_id
from argus.capsule.base import CapsuleError


_RUNTIME_ID = "runtime-sha256-" + "a" * 64


def _runtime_identity(path: Path, value: str = _RUNTIME_ID) -> Path:
    path.write_text(
        json.dumps({"runtime_identity": value}),
        encoding="utf-8",
    )
    return path


def test_bootstrap_service_stages_media_and_rejects_replay(
    tmp_path: Path,
) -> None:
    attempt = create_bootstrap_attempt(
        tmp_path / "attempts",
        capsule_id=new_capsule_id(),
        control_generation=2,
        execution_mode="isolated",
        runtime_identity=_RUNTIME_ID,
        session_id="session-two",
    )
    runtime = _runtime_identity(tmp_path / "runtime.json")
    control = tmp_path / "guest" / "control.json"
    try:
        prepared = prepare_bootstrap_service(
            bootstrap_root=attempt.root,
            runtime_identity_file=runtime,
            control_state_file=control,
            staging_parent=tmp_path / "staging",
        )
        assert prepared.manifest.control_generation == 2
        assert prepared.token_path.read_text(encoding="utf-8").strip()
        prepared.control_state_store.commit_generation(
            capsule_id=prepared.manifest.capsule_id,
            generation=2,
            session_id="session-two",
            execution_mode="isolated",
        )
        prepared.cleanup_all_staging()

        with pytest.raises(CapsuleError, match="stale"):
            prepare_bootstrap_service(
                bootstrap_root=attempt.root,
                runtime_identity_file=runtime,
                control_state_file=control,
                staging_parent=tmp_path / "staging",
            )
    finally:
        attempt.destroy()


def test_bootstrap_service_rejects_runtime_identity_mismatch(
    tmp_path: Path,
) -> None:
    attempt = create_bootstrap_attempt(
        tmp_path / "attempts",
        capsule_id=new_capsule_id(),
        control_generation=1,
        execution_mode="isolated",
        runtime_identity=_RUNTIME_ID,
    )
    runtime = _runtime_identity(
        tmp_path / "runtime.json",
        "runtime-sha256-" + "b" * 64,
    )
    try:
        with pytest.raises(CapsuleError, match="runtime identity"):
            prepare_bootstrap_service(
                bootstrap_root=attempt.root,
                runtime_identity_file=runtime,
                control_state_file=tmp_path / "guest" / "control.json",
                staging_parent=tmp_path / "staging",
            )
    finally:
        attempt.destroy()
