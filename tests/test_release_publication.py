"""Exercise installer publication without a GitHub token or native builders."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github/scripts/prepare_release_assets.py"
VERSION = "0.1.8"
# Independently specified release contract, not imported from the staging code.
ASSETS = (
    "Argus-0.1.8-windows-x64-portable.zip",
    "Argus-0.1.8-windows-x64-setup.exe",
    "Argus-0.1.8-windows-x64.msi",
    "Argus-0.1.8-windows-arm64-portable.zip",
    "Argus-0.1.8-windows-arm64-setup.exe",
    "Argus-0.1.8-windows-arm64.msi",
    "Argus-0.1.8-macos-universal2.zip",
    "Argus-0.1.8-macos-universal2.dmg",
    "Argus-0.1.8-linux-x86_64.AppImage",
    "Argus-0.1.8-linux-x86_64.deb",
    "Argus-0.1.8-linux-x86_64.rpm",
    "Argus-0.1.8-linux-x86_64.pkg.tar.zst",
    "Argus-0.1.8-linux-aarch64.AppImage",
    "Argus-0.1.8-linux-aarch64.deb",
    "Argus-0.1.8-linux-aarch64.rpm",
    "Argus-0.1.8-linux-aarch64.pkg.tar.zst",
    "argus_app_testing-0.1.8-py3-none-any.whl",
    "argus_app_testing-0.1.8.tar.gz",
)


def make_assets(source: Path) -> None:
    for name in ASSETS:
        # upload-artifact preserves the packages/ directory for Linux's mixed paths.
        parent = source / "packages" if name.endswith((".deb", ".rpm", ".zst")) else source
        parent.mkdir(parents=True, exist_ok=True)
        (parent / name).write_bytes(f"package contents: {name}".encode())


def stage(source: Path, destination: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), str(source), str(destination), "--version", VERSION],
        text=True, capture_output=True,
    )


def test_stage_flattens_every_installer_and_hashes_the_actual_bytes(tmp_path):
    source, destination = tmp_path / "download", tmp_path / "upload"
    make_assets(source)
    result = stage(source, destination)
    assert result.returncode == 0, result.stderr
    assert {p.name for p in destination.iterdir()} == set(ASSETS) | {"SHA256SUMS"}
    lines = (destination / "SHA256SUMS").read_text().splitlines()
    assert len(lines) == 18
    assert [line.split("  ")[1] for line in lines] == sorted(ASSETS)
    for line in lines:
        digest, name = line.split("  ")
        payload = f"package contents: {name}".encode()
        assert (destination / name).read_bytes() == payload
        assert digest == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("problem", ["missing", "duplicate", "wrong-version", "extra", "empty"])
def test_invalid_payload_fails_before_creating_upload_directory(tmp_path, problem):
    source, destination = tmp_path / "download", tmp_path / "upload"
    make_assets(source)
    package = source / "packages" / "Argus-0.1.8-linux-x86_64.deb"
    if problem == "missing":
        package.unlink()
    elif problem == "duplicate":
        (source / package.name).write_bytes(b"conflicting package")
    elif problem == "wrong-version":
        package.rename(package.with_name("Argus-0.1.7-linux-x86_64.deb"))
    elif problem == "extra":
        (source / "unintended-file.txt").write_text("not a release asset")
    else:
        package.write_bytes(b"")
    result = stage(source, destination)
    assert result.returncode != 0
    assert "Invalid release assets:" in result.stderr
    assert not destination.exists()


@pytest.mark.parametrize("workflow", ["release.yml", "release-on-merge.yml"])
def test_workflow_rejects_duplicate_paths_from_separate_artifacts(tmp_path, workflow):
    document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    download = next(step for step in document["jobs"]["publish-release"]["steps"]
                    if step.get("uses", "").startswith("actions/download-artifact@"))
    artifacts = tmp_path / "artifacts"
    make_assets(artifacts / "release-packages")
    duplicate = artifacts / "release-conflict/packages/Argus-0.1.8-linux-x86_64.deb"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_bytes(b"different bytes from a second artifact")
    source, destination = tmp_path / download["with"]["path"], tmp_path / "upload"
    # Model the download action's documented external boundary: merged artifacts
    # overwrite equal relative paths; separate artifacts keep their directories.
    for artifact in sorted(artifacts.iterdir()):
        target = source if download["with"].get("merge-multiple", False) else source / artifact.name
        shutil.copytree(artifact, target, dirs_exist_ok=True)
    result = stage(source, destination)
    assert result.returncode != 0, "download layout hid a conflicting package before validation"
    assert "duplicate asset name:" in result.stderr
    assert not destination.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="production publisher runs Bash on Ubuntu")
@pytest.mark.parametrize("workflow", ["release.yml", "release-on-merge.yml"])
@pytest.mark.parametrize("existing", [False, True])
def test_workflow_publishes_all_assets_without_a_git_checkout(tmp_path, workflow, existing):
    document = yaml.safe_load((ROOT / ".github/workflows" / workflow).read_text())
    job = document["jobs"]["publish-release"]
    publish = next(step for step in job["steps"] if step.get("name") == "Create or refresh GitHub Release")
    source = tmp_path / "release-assets"
    make_assets(source / "release-packages")
    staging = next(step for step in job["steps"] if step.get("name") == "Validate and stage release assets")
    script_dir = tmp_path / ".github/scripts"
    script_dir.mkdir(parents=True)
    shutil.copyfile(SCRIPT, script_dir / SCRIPT.name)
    # GitHub itself is the external boundary. Run the actual workflow shell while
    # the fake CLI rejects the wrong repository, missing files and directory uploads.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake = fake_bin / "gh"
    fake.write_text(
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "repo = os.environ.get('GH_REPO')\n"
        "for flag in ('--repo', '-R'):\n"
        "    if flag in args:\n"
        "        i = args.index(flag); repo = args[i+1]; del args[i:i+2]\n"
        "if repo != 'Varun-SV/Argus': sys.exit('repository context is missing')\n"
        "if args[:2] == ['release', 'view']: sys.exit(0 if os.environ['EXISTING'] == '1' else 1)\n"
        "expected = ['release', 'upload' if os.environ['EXISTING'] == '1' else 'create', 'v0.1.8']\n"
        "if args[:3] != expected: sys.exit('wrong release command or tag')\n"
        "files = []\n"
        "for arg in args[3:]:\n"
        "    if arg.startswith('release-'):\n"
        "        p = pathlib.Path(arg)\n"
        "        if not p.is_file(): sys.exit('not a file: ' + arg)\n"
        "        files.append(p.name)\n"
        "pathlib.Path('uploaded.txt').write_text('\\n'.join(sorted(files)))\n"
    )
    fake.chmod(0o755)
    env = {**os.environ,
           "PATH": os.pathsep.join((str(fake_bin), str(Path(sys.executable).parent), os.environ["PATH"])),
           "TAG": "v0.1.8", "VERSION": VERSION,
           "EXISTING": str(int(existing)), "GH_TOKEN": "test-only"}
    if "GH_REPO" in job.get("env", {}) or "GH_REPO" in publish.get("env", {}):
        env["GH_REPO"] = "Varun-SV/Argus"
    else:
        env.pop("GH_REPO", None)
    staged = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", staging["run"]],
                            cwd=tmp_path, env=env, text=True, capture_output=True)
    assert staged.returncode == 0, staged.stderr
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", publish["run"]],
                            cwd=tmp_path, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "uploaded.txt").read_text().splitlines() == sorted((*ASSETS, "SHA256SUMS"))
