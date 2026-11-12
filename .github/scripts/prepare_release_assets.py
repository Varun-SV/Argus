#!/usr/bin/env python3
"""Validate and flatten downloaded installer artifacts before publication."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import shutil


def prepare(source: Path, destination: Path, version: str) -> None:
    """Copy the complete tagged release payload and write its SHA-256 manifest."""
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError(f"invalid release version: {version}")

    expected = {
        f"Argus-{version}-windows-{arch}{suffix}"
        for arch in ("x64", "arm64")
        for suffix in ("-portable.zip", "-setup.exe", ".msi")
    } | {
        f"Argus-{version}-macos-universal2.{suffix}" for suffix in ("zip", "dmg")
    } | {
        f"Argus-{version}-linux-{arch}.{suffix}"
        for arch in ("x86_64", "aarch64")
        for suffix in ("AppImage", "deb", "rpm", "pkg.tar.zst")
    } | {
        f"argus_app_testing-{version}-py3-none-any.whl",
        f"argus_app_testing-{version}.tar.gz",
    }

    files: dict[str, Path] = {}
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"unexpected symlink: {path}")
        if not path.is_file():
            continue
        if path.name in files:
            raise ValueError(f"duplicate asset name: {path.name}")
        if path.stat().st_size == 0:
            raise ValueError(f"empty asset: {path.name}")
        files[path.name] = path
    missing, unexpected = expected - files.keys(), files.keys() - expected
    if missing or unexpected:
        raise ValueError(f"missing={sorted(missing)}; unexpected={sorted(unexpected)}")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError(f"destination must be empty: {destination}")

    destination.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, path in sorted(files.items()):
        target = destination / name
        shutil.copyfile(path, target)
        digest = hashlib.sha256()
        with target.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        manifest.append(f"{digest.hexdigest()}  {name}\n")
    (destination / "SHA256SUMS").write_text("".join(manifest), encoding="utf-8")
    print(f"Prepared {len(files)} release assets and SHA256SUMS in {destination}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    try:
        prepare(args.source, args.destination, args.version)
    except (OSError, ValueError) as error:
        raise SystemExit(f"Invalid release assets: {error}") from error


if __name__ == "__main__":
    main()
