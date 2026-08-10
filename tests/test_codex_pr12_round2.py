from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from argus.adapters.base import AdapterError
from argus.adapters.windows_gui import _windows_launch_args
from argus.capsule.base import CapsuleError
from argus.capsule.guest_agent import GuestAgentState
from argus.capsule.safe_open import open_workspace_regular_file


def test_workspace_read_handle_stays_bound_when_path_is_replaced(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    artifact = root / "artifact.bin"
    moved = root / "artifact-original.bin"
    artifact.write_bytes(b"original")

    with open_workspace_regular_file(root, "artifact.bin") as handle:
        artifact.rename(moved)
        artifact.write_bytes(b"replacement")
        assert handle.read() == b"original"


def test_workspace_read_rejects_symlink_escape_when_supported(tmp_path):
    if os.name == "nt":
        pytest.skip("Windows reparse-point behavior is covered by the handle implementation")

    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"secret")
    (root / "artifact.txt").symlink_to(outside)

    with pytest.raises(CapsuleError):
        with open_workspace_regular_file(root, "artifact.txt"):
            pass


def test_snapshot_copy_is_bounded_to_preflight_size(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    state = GuestAgentState()
    state.begin_files("growthbound123")
    artifact = state.workspace_root / "logs" / "growing.log"
    artifact.parent.mkdir(parents=True)
    original = b"a" * (2 * 1024 * 1024)
    artifact.write_bytes(original)

    real_open = open_workspace_regular_file
    write_blocked = [False]

    @contextmanager
    def growing_open(root: Path, relative: str):
        with real_open(root, relative) as inner:
            class GrowingHandle:
                def __init__(self):
                    self._first = True

                def fileno(self):
                    return inner.fileno()

                def seek(self, offset, whence=0):
                    return inner.seek(offset, whence)

                def read(self, size=-1):
                    data = inner.read(size)
                    if self._first:
                        self._first = False
                        try:
                            with artifact.open("ab") as writer:
                                writer.write(b"growth-after-preflight")
                                writer.flush()
                        except PermissionError:
                            # The supported Windows Capsule path holds a mandatory
                            # LockFileEx barrier, so the hostile append is refused
                            # before it can race the snapshot.
                            write_blocked[0] = True
                    return data

            yield GrowingHandle()

    monkeypatch.setattr(
        "argus.capsule.guest_agent.open_workspace_regular_file",
        growing_open,
    )

    if os.name == "nt":
        info = state.collect_info("logs/growing.log")
        assert write_blocked[0]
        assert info["size"] == len(original)
        assert bytes(state._collection_snapshots["logs/growing.log"]["data"]) == original
    else:
        with pytest.raises(AdapterError, match="changed while being snapshotted"):
            state.collect_info("logs/growing.log")
        assert "logs/growing.log" not in state._collection_snapshots


def test_collection_snapshot_aggregate_cap_is_enforced_before_next_copy(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr("argus.capsule.guest_agent.TRANSFER_MAX_TOTAL_BYTES", 5)
    monkeypatch.setattr("argus.capsule.guest_agent.TRANSFER_MAX_FILE_BYTES", 10)

    state = GuestAgentState()
    state.begin_files("aggregate123")
    first = state.workspace_root / "first.bin"
    second = state.workspace_root / "second.bin"
    first.write_bytes(b"abc")
    second.write_bytes(b"def")

    state.collect_info("first.bin")
    with pytest.raises(AdapterError, match="snapshot.*session limit"):
        state.collect_info("second.bin")

    assert list(state._collection_snapshots) == ["first.bin"]
    assert state._collection_snapshots["first.bin"]["size"] == 3


def test_literal_windows_gui_launch_keeps_spaced_path_as_one_argv_element():
    target = r"C:\Users\Test User\Argus Workspace\app.exe"

    assert _windows_launch_args(target, literal=True) == [target]
    assert len(_windows_launch_args(target, literal=False)) > 1


def test_guest_literal_launch_uses_adapter_literal_entrypoint(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    target = r"C:\Users\Test User\Argus Workspace\app.exe"

    class FakeGUI:
        def __init__(self):
            self.literal = None

        def set_working_directory(self, value):
            self.cwd = value

        def launch(self, value):
            raise AssertionError("normal command-string launch must not be used")

        def launch_literal(self, value):
            self.literal = value

        def capabilities(self):
            return {"actions": {"wait": {}, "done": {}}}

        def close(self):
            pass

    fake = FakeGUI()
    monkeypatch.setattr(
        "argus.capsule.guest_agent.create_platform_adapter",
        lambda adapter_type: fake,
    )

    state = GuestAgentState()
    state.begin_files("literalgui123")
    state.start("desktop-gui", target, "safe", literal_target=True)

    assert fake.literal == target
    assert fake.cwd == str(state.workspace_root)
