"""The packaged builder runs independently of checkout-only scripts."""

from hashlib import sha256
import json
from pathlib import Path
import sys
from types import ModuleType

import pytest

import argus
from argus.provisioning import build_guest_runtime as builder
from argus.provisioning.model import GuestRuntimeIdentity
from argus.provisioning.runtime_bundle import verify_guest_runtime_bundle


@pytest.mark.parametrize("system,target_os,entrypoint", [
    ("Windows", "windows-11", "argus-guest.exe"),
    ("Linux", "ubuntu", "argus-guest"),
])
def test_packaged_builder_freezes_and_pins_native_bundle(
    tmp_path, monkeypatch, capsys, system, target_os, entrypoint
):
    monkeypatch.setattr(builder.platform, "system", lambda: system)
    monkeypatch.setattr(builder.platform, "machine", lambda: "x86_64")
    hooks = ModuleType("PyInstaller.utils.hooks")
    hooks.collect_submodules = lambda name: [name + ".fixture"]
    monkeypatch.setitem(sys.modules, "PyInstaller.utils.hooks", hooks)
    commands = []

    def freeze(command, check):
        assert check is True
        commands.append(command)
        source = Path(command[-1]).read_text(encoding="utf-8")
        assert "argus.capsule.runtime_entrypoint" in source
        payload = Path(command[command.index("--distpath") + 1]) / "argus-guest"
        payload.mkdir(parents=True)
        (payload / entrypoint).write_bytes(b"frozen runtime fixture")

    monkeypatch.setattr(builder.subprocess, "run", freeze)
    output = tmp_path / "runtime.zip"
    builder.main([str(output), "--runtime-version", "0.1.0"])
    result = json.loads(capsys.readouterr().out)
    assert result["target_os"] == target_os
    assert result["runtime_bundle_sha256"] == sha256(output.read_bytes()).hexdigest()
    assert commands[0][commands[0].index("--paths") + 1] == str(Path(argus.__file__).resolve().parent.parent)
    verified = verify_guest_runtime_bundle(GuestRuntimeIdentity(
        bundle_path=str(output), runtime_bundle_sha256=result["runtime_bundle_sha256"],
        runtime_version="0.1.0", target_os=target_os,
    ))
    assert verified.manifest.entrypoint == entrypoint


def test_legacy_builder_delegates_to_packaged_main():
    from scripts.build_guest_runtime import main

    assert main is builder.main
