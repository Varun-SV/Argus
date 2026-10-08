"""PEP 517 build backend for the ``argus-next`` preview distribution, and the CLI staging tool.

Why this exists (docs/rearchitecture/p0-packaging-findings.md): maturin builds either a PyO3
extension (``bindings = "pyo3"``) or Rust binaries (``bindings = "bin"``) into a wheel, not both
from one crate. The distribution needs both (spec section 10): the native ``argus-next`` command
on ``PATH`` and the ``argus_next._native`` extension. The layout that works is:

* maturin builds the extension from ``crates/argus-py`` with ``bindings = "pyo3"``;
* the binary from ``crates/argus-cli`` is built first and staged into the wheel data directory
  ``argus_next.data/scripts/``; maturin packs it as ``argus_next-<ver>.data/scripts/argus-next``,
  which installers put next to the interpreter's console scripts.

This module wraps maturin's PEP 517 hooks so that ``pip install`` from the sdist (or from the
source tree) builds and stages the binary before maturin runs, and adds the ``argus-cli`` crate to
the sdist (maturin's sdist contains only the path dependencies of ``argus-py``).

CI builds wheels with the maturin command line instead (cross targets, zig, universal2) and calls
``python argus_next_build.py stage-cli ...`` first; see .github/workflows/rust-wheels.yml.

Standard library only: the module runs inside an isolated PEP 517 build environment on Python
3.10, which has maturin but no TOML writer (and no ``tomllib``).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

BINARY_STEM = "argus-next"
CLI_PACKAGE = "argus-cli"
#: Location of the CLI crate relative to the Cargo workspace root (same in the repo and sdist).
CLI_CRATE_DIR = PurePosixPath("crates/argus-cli")
DATA_DIR_NAME = "argus_next.data"
#: Opt-out for builds whose binary was staged beforehand (cross builds driven through PEP 517).
PRESTAGED_ENV = "ARGUS_NEXT_CLI_PRESTAGED"
#: Directories never copied from the CLI crate into the sdist.
_SDIST_SKIP_DIRS = {"target", "__pycache__", ".git"}

PROJECT_DIR = Path(__file__).resolve().parent.parent


class BuildError(RuntimeError):
    """The native binary could not be built or staged."""


# --------------------------------------------------------------------------------------------
# Locating the Cargo workspace and the binary
# --------------------------------------------------------------------------------------------


def manifest_path(project_dir: Path = PROJECT_DIR) -> Path:
    """Return the argus-py ``Cargo.toml`` named by ``[tool.maturin] manifest-path``."""
    text = (project_dir / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^manifest-path\s*=\s*"([^"]+)"\s*$', text, flags=re.MULTILINE)
    if match is None:
        raise BuildError("pyproject.toml has no [tool.maturin] manifest-path")
    return (project_dir / match.group(1)).resolve()


def workspace_root(project_dir: Path = PROJECT_DIR) -> Path:
    """Return the root directory of the Cargo workspace that contains argus-py."""
    output = _run(
        [
            "cargo",
            "locate-project",
            "--workspace",
            "--message-format",
            "plain",
            "--manifest-path",
            str(manifest_path(project_dir)),
        ],
        capture=True,
    )
    return Path(output.strip()).parent


def binary_name(target: str | None = None) -> str:
    """Return the binary file name for a Rust target triple (default: this host)."""
    windows = "windows" in target if target else os.name == "nt"
    return BINARY_STEM + (".exe" if windows else "")


def data_scripts_dir(project_dir: Path = PROJECT_DIR) -> Path:
    """Return the staging directory packed by maturin as ``<dist>-<ver>.data/scripts``."""
    return project_dir / DATA_DIR_NAME / "scripts"


def _run(cmd: list[str], *, capture: bool = False, cwd: Path | None = None) -> str:
    print("+ " + " ".join(cmd), file=sys.stderr, flush=True)
    try:
        completed = subprocess.run(
            cmd,
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE if capture else None,
            text=True,
        )
    except FileNotFoundError as exc:
        raise BuildError(f"{cmd[0]} is not installed; a Rust toolchain is required") from exc
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"command failed with exit code {exc.returncode}: {cmd}") from exc
    return completed.stdout or ""


def _executable_from_cargo_messages(messages: str) -> Path:
    """Return the ``argus-next`` executable reported by ``--message-format=json`` output."""
    found: Path | None = None
    for line in messages.splitlines():
        if not line.startswith("{"):
            continue
        message = json.loads(line)
        if message.get("reason") != "compiler-artifact":
            continue
        if message.get("target", {}).get("name") == BINARY_STEM and message.get("executable"):
            found = Path(message["executable"])
    if found is None:
        raise BuildError(f"cargo did not report an executable for {BINARY_STEM}")
    return found


def build_cli(
    *,
    target: str | None = None,
    zig_glibc: str | None = None,
    universal2: bool = False,
    locked: bool = False,
    project_dir: Path = PROJECT_DIR,
) -> Path:
    """Build the ``argus-next`` binary in release mode and return its path.

    Args:
        target: Rust target triple; ``None`` builds for the host (or ``CARGO_BUILD_TARGET``).
        zig_glibc: build with ``cargo zigbuild`` against this glibc version (for example
            ``"2.28"`` for ``manylinux_2_28``). Needs ``cargo-zigbuild`` and ``ziglang``.
        universal2: build ``x86_64`` and ``aarch64`` macOS binaries and join them with ``lipo``.
        locked: pass ``--locked`` (repository builds; the trimmed sdist workspace cannot).
    """
    root = workspace_root(project_dir)
    if universal2:
        parts = [
            build_cli(target=arch, locked=locked, project_dir=project_dir)
            for arch in ("x86_64-apple-darwin", "aarch64-apple-darwin")
        ]
        out_dir = root / "target" / "universal2-apple-darwin" / "release"
        out_dir.mkdir(parents=True, exist_ok=True)
        output = out_dir / BINARY_STEM
        _run(["lipo", "-create", "-output", str(output), *map(str, parts)])
        return output

    cmd = ["cargo", "zigbuild" if zig_glibc else "build", "--release"]
    if locked:
        cmd.append("--locked")
    cmd += ["--manifest-path", str(root / "Cargo.toml"), "-p", CLI_PACKAGE, "--bin", BINARY_STEM]
    if zig_glibc and not target:
        raise BuildError("--zig-glibc needs an explicit --target")
    if target:
        cmd += ["--target", f"{target}.{zig_glibc}" if zig_glibc else target]
    cmd.append("--message-format=json-render-diagnostics")
    return _executable_from_cargo_messages(_run(cmd, capture=True))


def stage_cli(binary: Path, project_dir: Path = PROJECT_DIR) -> Path:
    """Copy ``binary`` into the wheel data scripts directory and mark it executable.

    Any previously staged binary is removed first, so a wheel never carries a stale file or both
    the POSIX and the Windows name.
    """
    if not binary.is_file():
        raise BuildError(f"no binary at {binary}")
    scripts = data_scripts_dir(project_dir)
    if scripts.exists():
        shutil.rmtree(scripts)
    scripts.mkdir(parents=True)
    name = BINARY_STEM + (".exe" if binary.suffix.lower() == ".exe" else "")
    staged = scripts / name
    shutil.copy2(binary, staged)
    mode = staged.stat().st_mode
    staged.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH | stat.S_IRGRP | stat.S_IROTH)
    return staged


def staged_binary(project_dir: Path = PROJECT_DIR) -> Path | None:
    """Return the staged binary, if one is present."""
    scripts = data_scripts_dir(project_dir)
    for name in (BINARY_STEM, BINARY_STEM + ".exe"):
        if (scripts / name).is_file():
            return scripts / name
    return None


def _ensure_cli_for_wheel() -> None:
    if os.environ.get(PRESTAGED_ENV) == "1":
        if staged_binary() is None:
            raise BuildError(
                f"{PRESTAGED_ENV}=1 but nothing is staged in {data_scripts_dir()}; "
                "run 'python build_backend/argus_next_build.py stage-cli' first"
            )
        return
    # Host build. Cargo keeps the versions recorded in Cargo.lock; every direct dependency is
    # pinned with '=' in the workspace manifest (spec section 14).
    stage_cli(build_cli())


# --------------------------------------------------------------------------------------------
# sdist: add the CLI crate that maturin does not include
# --------------------------------------------------------------------------------------------


def add_member_to_workspace_manifest(text: str, member: str) -> str:
    """Return ``text`` with ``member`` added to ``[workspace] members``.

    The manifest in the sdist is written by maturin, which lists the members it kept on one
    ``members = [...]`` assignment. A member that is already listed is left alone.
    """
    pattern = re.compile(r"^(members\s*=\s*\[)(.*?)(\])", flags=re.MULTILINE | re.DOTALL)
    match = pattern.search(text)
    if match is None:
        raise BuildError("workspace manifest has no members list")
    members = re.findall(r'"([^"]+)"', match.group(2))
    if member in members:
        return text
    members.append(member)
    rendered = ", ".join(f'"{m}"' for m in sorted(members))
    return text[: match.start()] + f"{match.group(1)}{rendered}{match.group(3)}" + text[match.end() :]


def _cli_crate_files(root: Path) -> list[Path]:
    crate = root / Path(CLI_CRATE_DIR)
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(crate):
        dirnames[:] = sorted(d for d in dirnames if d not in _SDIST_SKIP_DIRS)
        files.extend(Path(dirpath) / f for f in sorted(filenames) if not f.endswith(".pyc"))
    return files


def add_cli_crate_to_sdist(sdist: Path, root: Path) -> None:
    """Rewrite the ``.tar.gz`` sdist in place with the CLI crate and workspace member added."""
    with tarfile.open(sdist, "r:gz") as source:
        members = source.getmembers()
        payload = {m.name: source.extractfile(m).read() for m in members if m.isfile()}
    top = members[0].name.split("/", 1)[0]
    manifest_name = f"{top}/Cargo.toml"
    if manifest_name not in payload:
        raise BuildError(f"sdist has no workspace manifest at {manifest_name}")
    reference = next(m for m in members if m.name == manifest_name)

    new_files: dict[str, bytes] = {}
    for path in _cli_crate_files(root):
        relative = path.relative_to(root).as_posix()
        new_files[f"{top}/{relative}"] = path.read_bytes()
    if f"{top}/{CLI_CRATE_DIR}/Cargo.toml" not in new_files:
        raise BuildError(f"{CLI_CRATE_DIR}/Cargo.toml not found under {root}")

    manifest = payload[manifest_name].decode("utf-8")
    payload[manifest_name] = add_member_to_workspace_manifest(
        manifest, CLI_CRATE_DIR.as_posix()
    ).encode("utf-8")

    temporary = sdist.with_suffix(".tmp")
    with tarfile.open(temporary, "w:gz", format=tarfile.PAX_FORMAT) as out:
        for member in members:
            if member.isfile():
                info = tarfile.TarInfo(member.name)
                info.size = len(payload[member.name])
                info.mode = member.mode
                info.mtime = member.mtime
                out.addfile(info, io.BytesIO(payload[member.name]))
            elif member.name not in new_files:
                out.addfile(member)
        for name in sorted(new_files):
            if name in payload:
                continue
            info = tarfile.TarInfo(name)
            info.size = len(new_files[name])
            info.mode = 0o644
            info.mtime = reference.mtime
            out.addfile(info, io.BytesIO(new_files[name]))
    temporary.replace(sdist)


# --------------------------------------------------------------------------------------------
# PEP 517 / PEP 660 hooks (delegating to maturin)
# --------------------------------------------------------------------------------------------


def _maturin():
    import maturin  # noqa: PLC0415 - only available inside the build environment

    return maturin


def get_requires_for_build_wheel(config_settings=None):
    return _maturin().get_requires_for_build_wheel(config_settings)


def get_requires_for_build_sdist(config_settings=None):
    return _maturin().get_requires_for_build_sdist(config_settings)


def get_requires_for_build_editable(config_settings=None):
    return _maturin().get_requires_for_build_editable(config_settings)


def prepare_metadata_for_build_wheel(metadata_directory, config_settings=None):
    return _maturin().prepare_metadata_for_build_wheel(metadata_directory, config_settings)


def prepare_metadata_for_build_editable(metadata_directory, config_settings=None):
    return _maturin().prepare_metadata_for_build_editable(metadata_directory, config_settings)


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    _ensure_cli_for_wheel()
    return _maturin().build_wheel(wheel_directory, config_settings, metadata_directory)


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    _ensure_cli_for_wheel()
    return _maturin().build_editable(wheel_directory, config_settings, metadata_directory)


def build_sdist(sdist_directory, config_settings=None):
    name = _maturin().build_sdist(sdist_directory, config_settings)
    add_cli_crate_to_sdist(Path(sdist_directory) / name, workspace_root())
    return name


# --------------------------------------------------------------------------------------------
# Command line (CI)
# --------------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    stage = commands.add_parser("stage-cli", help="build argus-next and stage it for the wheel")
    stage.add_argument("--target", help="Rust target triple (default: host)")
    stage.add_argument("--zig-glibc", help="glibc version for cargo zigbuild, e.g. 2.28")
    stage.add_argument("--universal2", action="store_true", help="macOS x86_64+arm64 binary")
    stage.add_argument("--locked", action="store_true", help="pass --locked to cargo")
    stage.add_argument("--binary", type=Path, help="stage this prebuilt binary instead")
    args = parser.parse_args(argv)

    try:
        if args.binary is not None:
            binary = args.binary
        else:
            binary = build_cli(
                target=args.target,
                zig_glibc=args.zig_glibc,
                universal2=args.universal2,
                locked=args.locked,
            )
        staged = stage_cli(binary)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(staged)
    return 0


if __name__ == "__main__":
    sys.exit(main())
