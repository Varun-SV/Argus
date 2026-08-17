from __future__ import annotations

import base64
import hashlib
import urllib.parse
from pathlib import Path

import pytest

from argus.adapters.base import Observation
from argus.capsule.base import (
    CapsuleError,
    CapsuleHandle,
    CapsuleProvider,
    CapsuleRequest,
    CapsuleSettings,
    FailureCapsule,
)
from argus.capsule.files import normalize_relative_path
from argus.capsule.guest_agent import GuestAgentState
from argus.engine.runner import run_test
from argus.engine.spec import StageFile, parse_spec
from argus.execution import CapsuleExecutionEnvironment
from argus.execution.base import ExecutionEnvironmentError
from tests.conftest import FakeProvider


_CAPABILITIES = {
    "actions": {"wait": {}, "done": {}},
    "notes": ["transfer fake"],
}


class TransferProvider(CapsuleProvider):
    provider_name = "fake-transfer"

    def __init__(self, root: Path):
        self.root = root
        self.created = []
        self.destroyed = []
        self.retained = []

    def create(self, request: CapsuleRequest) -> CapsuleHandle:
        self.created.append(request)
        self.root.mkdir(parents=True, exist_ok=True)
        return CapsuleHandle(
            session_id=request.session_id,
            provider=self.provider_name,
            vm_name="Argus-transfer-test",
            root_dir=str(self.root),
            address="10.10.0.2",
            guest_port=request.settings.guest_port,
        )

    def retain_failure(self, handle: CapsuleHandle, reason: str) -> FailureCapsule:
        self.retained.append((handle, reason))
        return FailureCapsule(
            failure_id=handle.session_id,
            session_id=handle.session_id,
            provider=self.provider_name,
            vm_name=handle.vm_name,
            root_dir=handle.root_dir,
            reason=reason,
            retained_at="2026-08-10T00:00:00+00:00",
            vm_state="Off",
        )

    def destroy(self, handle: CapsuleHandle) -> None:
        self.destroyed.append(handle)


class TransferClient:
    def __init__(self, artifact_data: bytes = b"artifact-data"):
        self.ready = False
        self.workspace = False
        self.launched = None
        self.closed = 0
        self.events = []
        self.artifact_data = artifact_data
        self.staged = {}

    def wait_until_ready(self, timeout_seconds: float) -> None:
        self.ready = True

    def begin_files(self, session_id: str) -> dict:
        self.workspace = True
        self.events.append(("begin", session_id))
        return {"workspace": f"C:/Argus/{session_id}"}

    def stage_file(self, source: Path, destination: str, expected_sha256: str = "") -> dict:
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if expected_sha256 and expected_sha256 != digest:
            raise CapsuleError("pinned checksum mismatch")
        self.staged[destination] = data
        self.events.append(("stage", destination))
        return {
            "guest_path": f"C:/Argus/session/{destination}",
            "size": len(data),
            "sha256": digest,
        }

    def collect_info(self, relative: str) -> dict:
        self.events.append(("collect-info", relative))
        return {
            "path": relative,
            "size": len(self.artifact_data),
            "sha256": hashlib.sha256(self.artifact_data).hexdigest(),
        }

    def _request(self, method: str, endpoint: str) -> dict:
        """Minimal chunk API used by the protected source->destination path."""
        assert method == "GET"
        parsed = urllib.parse.urlparse(endpoint)
        query = urllib.parse.parse_qs(parsed.query)
        relative = query["path"][0]
        offset = int(query["offset"][0])
        limit = int(query["limit"][0])
        if offset == 0:
            self.events.append(("collect", relative))
        chunk = self.artifact_data[offset : offset + limit]
        return {"data_b64": base64.b64encode(chunk).decode("ascii")}

    def collect_file(self, relative: str, output_root: Path, info=None) -> dict:
        """Legacy direct-environment collection kept for compatibility tests."""
        self.events.append(("collect", relative))
        output = output_root / Path(*relative.split("/"))
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(self.artifact_data)
        metadata = info or self.collect_info(relative)
        return {
            "path": relative,
            "size": metadata["size"],
            "sha256": metadata["sha256"],
            "host_path": str(output),
        }

    def launch(
        self,
        adapter_type: str,
        target: str,
        input_mode: str,
        *,
        literal_target: bool = False,
    ) -> dict:
        self.launched = (adapter_type, target, input_mode, literal_target)
        self.events.append(("launch", target, literal_target))
        return {"ok": True, "capabilities": _CAPABILITIES}

    def observe(self, include_screenshot: bool = True) -> Observation:
        return Observation(
            window_title="Transfer App",
            process_alive=True,
            action_capabilities=_CAPABILITIES,
        )

    def act(self, action: dict) -> str:
        return "acted"

    def close_session(self) -> None:
        self.events.append(("close", None))
        self.closed += 1


class FailingCollectClient(TransferClient):
    def collect_info(self, relative: str) -> dict:
        self.events.append(("collect-info", relative))
        raise CapsuleError("declared artifact is missing")


def _environment(tmp_path: Path, *, retain=False, client=None):
    provider = TransferProvider(tmp_path / "capsule")
    client = client or TransferClient()
    settings = CapsuleSettings(
        provider="hyperv",
        image="unused.vhdx",
        switch_name="Argus Internal",
        guest_token="secret",
        retain_on_failure=retain,
    )
    environment = CapsuleExecutionEnvironment(
        "desktop-gui",
        settings,
        provider=provider,
        client_factory=lambda *args, **kwargs: client,
        session_id="transfer123",
    )
    return environment, provider, client


def _protected_artifact_file(tmp_path: Path, result):
    assert result.ates_run_id
    root = tmp_path / ".argus" / "runs" / result.ates_run_id / "artifacts"
    files = sorted(path for path in root.rglob("*") if path.is_file())
    assert len(files) == 1
    return files[0]


def test_transfer_path_policy_rejects_host_or_guest_escape():
    assert normalize_relative_path(r"folder\file.txt") == "folder/file.txt"
    for value in ("../secret.txt", "/etc/passwd", r"C:\secret.txt", "a/../b"):
        with pytest.raises(CapsuleError):
            normalize_relative_path(value)


def test_guest_state_stages_and_collects_only_inside_workspace(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    state = GuestAgentState()
    state.begin_files("session123")
    payload = b"hello capsule"
    digest = hashlib.sha256(payload).hexdigest()

    state.stage_begin("app/payload.bin", len(payload), digest)
    state.stage_chunk("app/payload.bin", 0, base64.b64encode(payload).decode("ascii"))
    committed = state.stage_commit("app/payload.bin", len(payload), digest)

    assert Path(committed["guest_path"]).read_bytes() == payload
    info = state.collect_info("app/payload.bin")
    assert info == {"size": len(payload), "sha256": digest}
    assert state.collect_chunk("app/payload.bin", 0, len(payload)) == payload
    with pytest.raises(CapsuleError):
        state.collect_info("../outside.txt")


def test_environment_stages_stage_uri_and_collects(tmp_path):
    source = tmp_path / "dist" / "app.exe"
    source.parent.mkdir()
    source.write_bytes(b"binary")
    environment, provider, client = _environment(tmp_path)

    environment.prepare_transfers()
    staged = environment.stage_files(
        [StageFile(source="dist/app.exe", destination="bin/app.exe")],
        tmp_path,
    )
    environment.launch("stage://bin/app.exe")
    artifacts = environment.collect_artifacts(["logs/result.txt"], tmp_path / "out")

    assert staged[0]["destination"] == "bin/app.exe"
    assert client.launched[1] == "C:/Argus/session/bin/app.exe"
    assert client.launched[3] is True
    assert artifacts[0]["path"] == "logs/result.txt"
    assert (tmp_path / "out" / "logs" / "result.txt").read_bytes() == b"artifact-data"
    environment.close()
    assert len(provider.destroyed) == 1


def test_undeclared_stage_uri_rolls_back_without_retention(tmp_path):
    environment, provider, _ = _environment(tmp_path, retain=True)
    environment.prepare_transfers()

    with pytest.raises(ExecutionEnvironmentError, match="not declared/committed"):
        environment.launch("stage://bin/missing.exe")

    assert provider.retained == []
    assert len(provider.destroyed) == 1
    assert environment._handle is None


def test_runner_collects_before_teardown_and_reports_manifest(tmp_path):
    source = tmp_path / "dist" / "app.exe"
    source.parent.mkdir()
    source.write_bytes(b"binary")
    environment, provider, client = _environment(tmp_path)
    spec = parse_spec(
        """\
name: transfer integration
staging:
  - source: dist/app.exe
    destination: bin/app.exe
target:
  adapter: desktop-gui
  launch: stage://bin/app.exe
steps:
  - assert:
      process_running: true
collect:
  - logs/result.txt
teardown:
  - close
"""
    )

    result = run_test(spec, FakeProvider([]), environment, project_dir=tmp_path)

    assert result.status == "pass"
    assert result.staged_files[0]["source"] == "dist/app.exe"
    assert result.artifacts[0]["protected"] is True
    assert "host_path" not in result.artifacts[0]
    assert "logs/result.txt" not in repr(result.artifacts)
    assert _protected_artifact_file(tmp_path, result).read_bytes() == b"artifact-data"
    collect_index = next(i for i, event in enumerate(client.events) if event[0] == "collect")
    close_index = next(i for i, event in enumerate(client.events) if event[0] == "close")
    assert collect_index < close_index
    assert len(provider.destroyed) == 1


def test_runner_collects_after_non_close_teardown_before_close(tmp_path):
    source = tmp_path / "app.exe"
    source.write_bytes(b"binary")
    environment, provider, client = _environment(tmp_path)
    spec = parse_spec(
        """\
name: teardown flush ordering
staging:
  - source: app.exe
    destination: app.exe
target:
  adapter: desktop-gui
  launch: stage://app.exe
steps:
  - assert:
      process_running: true
collect:
  - logs/result.txt
teardown:
  - flush logs
  - close
"""
    )

    def on_step(step):
        client.events.append(("step-result", step.text))
        if step.text == "flush logs":
            client.artifact_data = b"flushed-artifact"

    result = run_test(
        spec,
        FakeProvider([]),
        environment,
        on_step=on_step,
        project_dir=tmp_path,
    )

    assert result.status == "pass"
    assert result.artifacts[0]["protected"] is True
    assert _protected_artifact_file(tmp_path, result).read_bytes() == b"flushed-artifact"
    flush_index = next(
        i for i, event in enumerate(client.events)
        if event == ("step-result", "flush logs")
    )
    collect_index = next(i for i, event in enumerate(client.events) if event[0] == "collect")
    close_index = next(i for i, event in enumerate(client.events) if event[0] == "close")
    assert flush_index < collect_index < close_index
    assert len(provider.destroyed) == 1


def test_collection_failure_arms_failure_capsule_before_teardown(tmp_path):
    source = tmp_path / "app.exe"
    source.write_bytes(b"binary")
    environment, provider, client = _environment(
        tmp_path,
        retain=True,
        client=FailingCollectClient(),
    )
    spec = parse_spec(
        """\
name: collect failure retention
staging:
  - source: app.exe
    destination: app.exe
target:
  adapter: desktop-gui
  launch: stage://app.exe
steps:
  - assert:
      process_running: true
collect:
  - logs/missing.txt
teardown:
  - close
"""
    )

    result = run_test(spec, FakeProvider([]), environment, project_dir=tmp_path)

    assert result.status == "error"
    assert "declared artifact is missing" in (result.transfer_error or "")
    assert result.failure_capsule is not None
    assert "artifact collection failed" in result.failure_capsule["reason"]
    assert len(provider.retained) == 1
    assert provider.destroyed == []
    assert client.closed == 0
