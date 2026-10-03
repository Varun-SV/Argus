"""Privilege boundary tests for the production guest adapter worker."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

import pytest

from argus.adapters.base import AdapterError
from argus.capsule.target_worker import TargetWorkerAdapter


@pytest.mark.parametrize("mode", ["isolated", "shared_user"])
def test_production_server_selects_unprivileged_worker_for_both_modes(mode):
    from argus.capsule.secure_guest_agent import SecureGuestAgentServer

    server = SecureGuestAgentServer(("127.0.0.1", 0), "bootstrap", capsule_id="cap-" + "a" * 32,
                                    execution_mode=mode, control_generation=1)
    try:
        adapter = server.state._adapter_factory("cli")
        assert isinstance(adapter, TargetWorkerAdapter)
        assert adapter.execution_mode == mode
        with pytest.raises(AdapterError, match="initialized file workspace"):
            adapter.launch("whoami")
    finally:
        server.server_close()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux account policy")
@pytest.mark.parametrize("group", ["sudo", "wheel", "root", "docker", "lxd", "incus", "libvirt"])
def test_worker_rejects_root_equivalent_target_groups(tmp_path, monkeypatch, group):
    import grp
    import pwd
    from argus.capsule.target_worker import _linux_target

    monkeypatch.setattr(pwd, "getpwnam", lambda name: SimpleNamespace(
        pw_name="argus", pw_uid=1001, pw_gid=1001, pw_dir=str(tmp_path)))
    monkeypatch.setattr(os, "getgrouplist", lambda name, gid: [1001])
    monkeypatch.setattr(grp, "getgrgid", lambda gid: SimpleNamespace(gr_name=group))
    with pytest.raises(AdapterError, match="must not be privileged"):
        _linux_target(tmp_path)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="POSIX descriptor ownership")
def test_workspace_ownership_walk_rejects_symlink_without_touching_outside(tmp_path, monkeypatch):
    from argus.capsule.target_worker import _authorize_linux_workspace

    outside = tmp_path / "outside"
    outside.write_text("outside")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "escape").symlink_to(outside)
    touched = []
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: touched.append(os.fstat(fd).st_ino))
    with pytest.raises(AdapterError, match="cannot be established safely"):
        _authorize_linux_workspace(workspace, 1001, 1001)
    assert outside.stat().st_ino not in touched


def test_launched_app_cannot_write_replies_into_worker_standard_handles(tmp_path):
    """Exercise real process/pipe inheritance independently of UID availability."""
    import subprocess

    source_root = str(Path(__file__).resolve().parents[1])
    app_probe = "import os; os.write(1, b'{\"ok\":true,\"result\":\"forged\"}\\n'); assert os.read(0, 1) == b''"
    driver = (
        "import sys, os, subprocess; sys.path.insert(0, " + repr(source_root) + ");"
        "import argus.adapters.base as base;"
        "from argus.capsule.target_worker import worker_main\n"
        "class Adapter:\n"
        " def set_working_directory(self, path): pass\n"
        " def launch(self, target):\n"
        "  subprocess.run([sys.executable, '-c', " + repr(app_probe) + "], check=True)\n"
        " def capabilities(self): return {'source': 'trusted-worker'}\n"
        " def close(self): pass\n"
        "base.create_adapter=lambda kind: Adapter()\n"
        + ("os.geteuid=lambda: 1001\n" if os.name != "nt" else "")
        + "worker_main()\n"
    )
    child = subprocess.Popen([sys.executable, "-c", driver], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    requests = (
        json.dumps({"operation": "start", "execution_mode": "isolated", "adapter_type": "cli",
                    "target": "probe", "literal_target": False,
                    "workspace": str(tmp_path), "input_mode": "safe"}) + "\n"
        + json.dumps({"operation": "close"}) + "\n"
    ).encode()
    stdout, stderr = child.communicate(requests, timeout=10)
    assert child.returncode == 0, stderr.decode()
    replies = [json.loads(line) for line in stdout.splitlines()]
    assert replies == [{"ok": True, "result": {"source": "trusted-worker"}},
                       {"ok": True, "result": None}]


@pytest.mark.skipif(not sys.platform.startswith("linux") or os.geteuid() != 0,
                    reason="requires root to exercise a real uid transition")
@pytest.mark.parametrize("mode", ["isolated", "shared_user"])
def test_real_worker_runs_as_nonadmin_and_cannot_read_control_material(monkeypatch, mode):
    import pwd
    from argus.capsule import target_worker

    account = pwd.getpwnam("nobody")
    mappings = [tuple(map(int, line.split())) for line in Path("/proc/self/uid_map").read_text().splitlines()]
    if not any(start <= account.pw_uid < start + length for start, _host, length in mappings):
        pytest.skip("execution namespace has no mapped non-admin UID")
    original_lookup = pwd.getpwnam
    monkeypatch.setattr(pwd, "getpwnam", lambda name: account if name == "argus" else original_lookup(name))
    source_root = str(Path(__file__).resolve().parents[1])
    monkeypatch.setattr(target_worker, "_worker_command", lambda: [sys.executable, "-c",
        "import sys; sys.path.insert(0, " + repr(source_root) + ");"
        "from argus.capsule.target_worker import worker_main; worker_main()"])
    monkeypatch.setenv("ARGUS_CAPSULE_GUEST_TOKEN", "control-token-sentinel")
    with tempfile.TemporaryDirectory(prefix="argus-worker-test-") as directory:
        root = Path(directory)
        root.chmod(0o755)
        protected = root / "protected"
        protected.mkdir(mode=0o700)
        secret = protected / "tls-key.pem"
        secret.write_text("private-key-sentinel")
        workspace = root / "target"
        workspace.mkdir()
        script = workspace / "probe.py"
        script.write_text(
            "import json, os\n"
            "print(json.dumps({'uid':os.geteuid(),'groups':os.getgroups(),"
            "'token':os.environ.get('ARGUS_CAPSULE_GUEST_TOKEN')}))\n"
            "try:\n open(" + repr(str(secret)) + ").read()\n"
            "except PermissionError:\n print('control_material_denied')\n"
        )
        adapter = TargetWorkerAdapter("cli", mode)
        adapter.set_working_directory(str(workspace))
        try:
            import shlex

            adapter.launch(shlex.join([sys.executable, str(script)]))
            observation = adapter.observe(False)
            assert observation.exit_code == 0, observation.stderr
            lines = observation.stdout.splitlines()
            identity = json.loads(lines[0])
            assert identity == {"uid": account.pw_uid, "groups": [], "token": None}
            assert lines[1] == "control_material_denied"
            action = adapter.prepare_action({"action": "run", "command": "/usr/bin/id -u"})
            adapter.dispatch_prepared_action(action)
            assert adapter.observe(False).stdout.strip() == str(account.pw_uid)
        finally:
            adapter.close()
