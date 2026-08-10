from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from argus.adapters.base import AdapterError
from argus.capsule.base import CapsuleError, CapsuleSettings
from argus.capsule.guest_agent import GuestAgentState
from argus.capsule.safe_output import pin_artifact_tree
from argus.engine.spec import SpecError, StageFile, parse_spec
from argus.execution import CapsuleExecutionEnvironment
from tests.test_capsule_transfers import TransferClient, TransferProvider


class ReplacingStageClient(TransferClient):
    def __init__(self, source_path: Path, replacement: Path):
        super().__init__()
        self.source_path = source_path
        self.replacement = replacement
        self.uploaded = None

    def stage_open_file(
        self,
        source_handle,
        source_name: str,
        destination: str,
        expected_sha256: str = "",
    ) -> dict:
        # Replace the authorized pathname only after the environment has opened
        # and bound the source. The upload must still read the original object.
        moved = self.source_path.with_suffix(".bound")
        self.source_path.rename(moved)
        try:
            self.source_path.symlink_to(self.replacement)
        except OSError:
            # Windows environments without symlink privilege still exercise the
            # path-replacement property with another regular file.
            self.source_path.write_bytes(self.replacement.read_bytes())

        source_handle.seek(0)
        self.uploaded = source_handle.read()
        digest = hashlib.sha256(self.uploaded).hexdigest()
        return {
            "guest_path": f"C:/Argus/session/{destination}",
            "size": len(self.uploaded),
            "sha256": digest,
        }


def test_staging_upload_stays_bound_after_source_path_replacement(tmp_path):
    source = tmp_path / "app.bin"
    outside = tmp_path.parent / f"{tmp_path.name}-outside.bin"
    source.write_bytes(b"authorized-original")
    outside.write_bytes(b"outside-replacement")

    provider = TransferProvider(tmp_path / "capsule")
    client = ReplacingStageClient(source, outside)
    environment = CapsuleExecutionEnvironment(
        "desktop-gui",
        CapsuleSettings(
            provider="hyperv",
            image="unused.vhdx",
            switch_name="Argus Internal",
            guest_token="secret",
        ),
        provider=provider,
        client_factory=lambda *args, **kwargs: client,
        session_id="stagerace123",
    )

    environment.stage_files(
        [StageFile(source="app.bin", destination="bin/app.bin")],
        tmp_path,
    )

    assert client.uploaded == b"authorized-original"
    environment.close()


def test_collect_rejects_case_only_windows_aliases():
    with pytest.raises(SpecError, match="Windows semantics"):
        parse_spec(
            """\
name: aliases
target:
  adapter: desktop-gui
  launch: app.exe
steps:
  - assert:
      process_running: true
collect:
  - logs/Result.txt
  - logs/result.txt
"""
        )


def test_staging_rejects_case_only_windows_destination_aliases():
    with pytest.raises(SpecError, match="Windows semantics"):
        parse_spec(
            """\
name: staging aliases
staging:
  - source: first.bin
    destination: Bin/App.exe
  - source: second.bin
    destination: bin/app.exe
target:
  adapter: desktop-gui
  launch: app.exe
steps:
  - assert:
      process_running: true
"""
        )


def test_same_size_rewrite_with_restored_mtime_cannot_corrupt_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    state = GuestAgentState()
    state.begin_files("tornsnapshot123")
    artifact = state.workspace_root / "logs" / "result.bin"
    artifact.parent.mkdir(parents=True)
    original = b"AAAA"
    artifact.write_bytes(original)
    original_stat = artifact.stat()

    real_handle = artifact.open("rb")
    write_blocked = [False]

    class MutatingHandle:
        def __init__(self, handle):
            self.handle = handle
            self.zero_seeks = 0

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            return self.handle.read(size)

        def seek(self, offset, whence=0):
            if offset == 0 and whence == 0:
                self.zero_seeks += 1
                if self.zero_seeks == 2:
                    try:
                        with artifact.open("r+b") as writer:
                            writer.seek(0)
                            writer.write(b"BBBB")
                            writer.flush()
                        os.utime(
                            artifact,
                            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                        )
                    except PermissionError:
                        # The supported Windows guest takes a mandatory byte-range
                        # lock, so the same-size rewrite is denied at the source.
                        write_blocked[0] = True
            return self.handle.seek(offset, whence)

        def close(self):
            self.handle.close()

    @contextmanager
    def fake_open(_root, _relative):
        wrapper = MutatingHandle(real_handle)
        try:
            yield wrapper
        finally:
            wrapper.close()

    monkeypatch.setattr(
        "argus.capsule.guest_agent.open_workspace_regular_file",
        fake_open,
    )

    if os.name == "nt":
        info = state.collect_info("logs/result.bin")
        assert write_blocked[0]
        assert info["sha256"] == hashlib.sha256(original).hexdigest()
        assert bytes(state._collection_snapshots["logs/result.bin"]["data"]) == original
    else:
        # Advisory POSIX locking cannot stop an uncooperative writer, so the
        # second bounded digest/ctime pass must reject the changed object.
        with pytest.raises((AdapterError, CapsuleError), match="snapshot|stabilized"):
            state.collect_info("logs/result.bin")
        assert "logs/result.bin" not in state._collection_snapshots


@pytest.mark.skipif(os.name != "nt", reason="Hyper-V host replacement barrier is Windows-specific")
def test_windows_pinned_run_directory_cannot_be_replaced(tmp_path):
    runs_root = tmp_path / ".argus" / "runs"
    run_dir = runs_root / "run-1"
    output = run_dir / "artifacts"
    run_dir.mkdir(parents=True)

    with pin_artifact_tree(output, ["logs/result.txt"]):
        replacement = tmp_path / "replacement-run"
        replacement.mkdir()
        with pytest.raises(OSError):
            os.replace(run_dir, tmp_path / "moved-run")


def test_pre_redirected_run_directory_is_rejected_before_artifact_write(tmp_path):
    runs_root = tmp_path / ".argus" / "runs"
    runs_root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    run_dir = runs_root / "run-1"
    try:
        run_dir.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this runner")

    output = run_dir / "artifacts"
    with pytest.raises(CapsuleError):
        with pin_artifact_tree(output, ["logs/result.txt"]):
            pass

    assert not (outside / "artifacts").exists()
