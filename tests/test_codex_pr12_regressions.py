from __future__ import annotations

import base64
import hashlib
import shlex
import subprocess
from pathlib import Path

import pytest

from argus.adapters.cli_adapter import CLIAdapter
from argus.capsule.base import CapsuleError, CapsuleSettings
from argus.capsule.guest import CapsuleGuestError, GuestAgentClient
from argus.capsule.guest_agent import GuestAgentState
from argus.engine.spec import SpecError, StageFile, parse_spec
from argus.execution import CapsuleExecutionEnvironment
from argus.execution.base import ExecutionEnvironmentError
from tests.test_capsule_transfers import TransferClient, TransferProvider


class WindowsPathClient(TransferClient):
    def stage_file(self, source: Path, destination: str, expected_sha256: str = "") -> dict:
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        return {
            "guest_path": r"C:\Users\guest\Argus Workspace\bin\app.exe",
            "size": len(data),
            "sha256": digest,
        }


def _cli_environment(tmp_path: Path, client: TransferClient):
    provider = TransferProvider(tmp_path / "capsule")
    settings = CapsuleSettings(
        provider="hyperv",
        image="unused.vhdx",
        switch_name="Argus Internal",
        guest_token="secret",
    )
    environment = CapsuleExecutionEnvironment(
        "cli",
        settings,
        provider=provider,
        client_factory=lambda *args, **kwargs: client,
        session_id="codexcli123",
    )
    return environment, provider


def test_staged_windows_cli_target_survives_posix_shlex(tmp_path):
    source = tmp_path / "app.exe"
    source.write_bytes(b"binary")
    client = WindowsPathClient()
    environment, provider = _cli_environment(tmp_path, client)

    environment.prepare_transfers()
    environment.stage_files(
        [StageFile(source="app.exe", destination="bin/app.exe")],
        tmp_path,
    )
    environment.launch("stage://bin/app.exe")

    forwarded = client.launched[1]
    assert shlex.split(forwarded) == [r"C:\Users\guest\Argus Workspace\bin\app.exe"]
    environment.close()
    assert len(provider.destroyed) == 1


def test_cli_adapter_uses_pinned_workspace_for_launch_and_later_actions(tmp_path, monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs.get("cwd")))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    adapter = CLIAdapter()
    adapter.set_working_directory(str(tmp_path))

    adapter.launch("first-command")
    adapter.act({"action": "run", "command": "second-command"})

    assert len(calls) == 2
    assert all(cwd == str(tmp_path) for _, cwd in calls)


def test_guest_state_pins_cli_workspace_before_launch(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))

    class FakeCLI:
        def __init__(self):
            self.cwd = None
            self.launch_cwd = None

        def set_working_directory(self, value):
            self.cwd = value

        def launch(self, target):
            self.launch_cwd = str(Path.cwd())

        def capabilities(self):
            return {"actions": {"run": {"command": "required"}}}

        def close(self):
            pass

    fake = FakeCLI()
    monkeypatch.setattr(
        "argus.capsule.guest_agent.create_platform_adapter",
        lambda adapter_type: fake,
    )
    original_cwd = Path.cwd()
    state = GuestAgentState()
    state.begin_files("workspacecli123")

    state.start("cli", "app.exe", "safe")

    assert fake.cwd == str(state.workspace_root)
    assert fake.launch_cwd == str(state.workspace_root)
    assert Path.cwd() == original_cwd


def test_collection_rejects_artifact_that_changes_after_preflight(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    output_root = run_dir / "artifacts"
    original = b"abc"
    original_hash = hashlib.sha256(original).hexdigest()
    changed = b"abcd"
    changed_hash = hashlib.sha256(changed).hexdigest()

    client = GuestAgentClient("http://127.0.0.1:8765", "token")

    def fake_request(method, path, payload=None):
        if path.startswith("/v1/files/collect/chunk"):
            return {"data_b64": base64.b64encode(original).decode("ascii")}
        raise AssertionError(f"unexpected request: {method} {path}")

    client._request = fake_request
    client.collect_info = lambda relative: {
        "path": relative,
        "size": len(changed),
        "sha256": changed_hash,
    }

    with pytest.raises(CapsuleGuestError, match="changed while being collected"):
        client.collect_file(
            "logs/result.txt",
            output_root,
            info={
                "path": "logs/result.txt",
                "size": len(original),
                "sha256": original_hash,
            },
        )

    assert not (output_root / "logs" / "result.txt").exists()


class LaterArtifactFailureClient(TransferClient):
    def collect_info(self, relative: str) -> dict:
        data = relative.encode("utf-8")
        return {
            "path": relative,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }

    def collect_file(self, relative: str, output_root: Path, info=None) -> dict:
        if relative == "second.txt":
            raise CapsuleError("second artifact failed")
        data = relative.encode("utf-8")
        output = output_root / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
        return {
            "path": relative,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "host_path": str(output),
        }


def test_later_collection_failure_removes_earlier_committed_artifacts(tmp_path):
    client = LaterArtifactFailureClient()
    provider = TransferProvider(tmp_path / "capsule")
    settings = CapsuleSettings(
        provider="hyperv",
        image="unused.vhdx",
        switch_name="Argus Internal",
        guest_token="secret",
    )
    environment = CapsuleExecutionEnvironment(
        "desktop-gui",
        settings,
        provider=provider,
        client_factory=lambda *args, **kwargs: client,
        session_id="collecttxn123",
    )
    environment.prepare_transfers()
    output_dir = tmp_path / "run" / "artifacts"

    with pytest.raises(ExecutionEnvironmentError, match="second artifact failed"):
        environment.collect_artifacts(["first.txt", "second.txt"], output_dir)

    assert not (output_dir / "first.txt").exists()
    environment.close()


@pytest.mark.parametrize(
    "collect_block, error",
    [
        ("  - ../escape.txt", "invalid"),
        ("  - C:\\\\escape.txt", "invalid"),
        ("  - logs/result.txt\n  - logs\\\\result.txt", "duplicates"),
    ],
)
def test_invalid_collection_declarations_fail_during_spec_parse(collect_block, error):
    text = f"""\
name: invalid collect
target:
  adapter: desktop-gui
  launch: app.exe
steps:
  - assert:
      process_running: true
collect:
{collect_block}
"""

    with pytest.raises(SpecError, match=error):
        parse_spec(text)
