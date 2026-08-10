from __future__ import annotations

import pytest

from argus.adapters.base import AdapterError
from argus.capsule.base import CapsuleError
from argus.capsule.files import (
    TRANSFER_MAX_FILE_BYTES,
    TRANSFER_MAX_TOTAL_BYTES,
    project_source_path,
)
from argus.capsule.guest import CapsuleGuestError, GuestAgentClient
from argus.capsule.guest_agent import GuestAgentState
from argus.engine.results import RunResult


def test_guest_pending_uploads_count_toward_session_limit(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    state = GuestAgentState()
    state.begin_files("quota123")
    digest = "0" * 64

    count = TRANSFER_MAX_TOTAL_BYTES // TRANSFER_MAX_FILE_BYTES
    for index in range(count):
        state.stage_begin(
            f"pending/file-{index}.bin",
            TRANSFER_MAX_FILE_BYTES,
            digest,
        )

    with pytest.raises(AdapterError, match="session byte limit"):
        state.stage_begin("pending/overflow.bin", 1, digest)


def test_host_artifact_output_root_symlink_is_rejected(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    output_root = tmp_path / "artifacts"
    try:
        output_root.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable on this runner: {exc}")

    client = GuestAgentClient("http://127.0.0.1:8765", "token")
    with pytest.raises(CapsuleGuestError, match="output root cannot be a symlink"):
        client.collect_file(
            "result.txt",
            output_root,
            info={
                "size": 0,
                "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            },
        )

    assert list(outside.iterdir()) == []


def test_host_artifact_nested_symlink_escape_is_rejected(tmp_path):
    output_root = tmp_path / "artifacts"
    output_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = output_root / "logs"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable on this runner: {exc}")

    client = GuestAgentClient("http://127.0.0.1:8765", "token")
    with pytest.raises(CapsuleError, match="escapes the session workspace"):
        client.collect_file(
            "logs/result.txt",
            output_root,
            info={
                "size": 0,
                "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            },
        )

    assert list(outside.iterdir()) == []


def test_host_staging_source_symlink_cannot_escape_project(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "secret.bin"
    outside.write_bytes(b"secret")
    link = project / "linked.bin"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable on this runner: {exc}")

    with pytest.raises(CapsuleError, match="escapes the project root"):
        project_source_path(project, "linked.bin")


def _result() -> RunResult:
    return RunResult(
        test_name="escape",
        test_file="escape.test.yaml",
        adapter="desktop-gui",
        provider="fake",
    )


def test_argus_root_redirect_is_rejected_before_external_run_creation(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside-argus"
    outside.mkdir()
    argus_dir = project / ".argus"
    try:
        argus_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable on this runner: {exc}")

    with pytest.raises(OSError, match=".argus cannot be a symlink"):
        _result().run_dir(project)

    assert list(outside.iterdir()) == []


def test_runs_root_redirect_is_rejected_before_external_write(tmp_path):
    project = tmp_path / "project"
    argus_dir = project / ".argus"
    argus_dir.mkdir(parents=True)
    outside = tmp_path / "outside-runs"
    outside.mkdir()
    runs = argus_dir / "runs"
    try:
        runs.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable on this runner: {exc}")

    with pytest.raises(OSError, match=".argus/runs cannot be a symlink"):
        _result().run_dir(project)

    assert list(outside.iterdir()) == []
