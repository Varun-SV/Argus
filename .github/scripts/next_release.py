#!/usr/bin/env python3
"""Calculate the next Argus release version without mutating the source tree."""

from __future__ import annotations

import argparse
import os
import re

SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def bump(version: str, kind: str) -> str:
    if not SEMVER_RE.fullmatch(version):
        raise SystemExit(f"current version is not SemVer X.Y.Z: {version}")
    major, minor, patch = (int(piece) for piece in version.split("."))
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise SystemExit(f"unsupported bump type: {kind}")


def write_output(name: str, value: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", required=True)
    parser.add_argument("--bump", choices=("major", "minor", "patch"), required=True)
    args = parser.parse_args()

    version = bump(args.current, args.bump)
    write_output("version", version)
    write_output("tag", f"v{version}")


if __name__ == "__main__":
    main()
