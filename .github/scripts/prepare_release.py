#!/usr/bin/env python3
"""Prepare a deterministic Argus release commit after a merged pull request.

The script is intentionally local-only: it mutates version/changelog/release
markers, validates that Argus' two version declarations agree, and writes
outputs for the calling GitHub Actions step when GITHUB_OUTPUT is available.
"""
from __future__ import annotations

import argparse
import os
import re
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "pyproject.toml"
INIT = ROOT / "argus" / "__init__.py"
CHANGELOG = ROOT / "CHANGELOG.md"
README = ROOT / "README.md"
SITE = ROOT / "index.html"
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def read_project_version() -> str:
    text = PYPROJECT.read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', text)
    if match is None:
        raise SystemExit("could not locate project version in pyproject.toml")
    version = match.group(1)
    if not SEMVER_RE.fullmatch(version):
        raise SystemExit(f"project version is not SemVer X.Y.Z: {version}")
    return version


def read_runtime_version() -> str:
    text = INIT.read_text(encoding="utf-8")
    match = re.search(r'(?m)^__version__\s*=\s*"([^"]+)"\s*$', text)
    if match is None:
        raise SystemExit("could not locate __version__ in argus/__init__.py")
    return match.group(1)


def bump(version: str, kind: str) -> str:
    major, minor, patch = (int(piece) for piece in version.split("."))
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    if kind == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise SystemExit(f"unsupported bump type: {kind}")


def replace_version(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    updated, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if count != 1:
        raise SystemExit(f"expected exactly one version declaration in {path}")
    path.write_text(updated, encoding="utf-8")


def release_changelog(new_version: str, pr_number: int, pr_title: str) -> str:
    text = CHANGELOG.read_text(encoding="utf-8")
    match = re.search(
        r"(?ms)^## \[Unreleased\]\s*\n(?P<body>.*?)(?=^## \[|\Z)",
        text,
    )
    if match is None:
        raise SystemExit("CHANGELOG.md is missing a ## [Unreleased] section")

    body = match.group("body").strip()
    if not body:
        body = f"### Changed\n- {pr_title.strip()} (#{pr_number})"
    released = (
        "## [Unreleased]\n\n"
        f"## [{new_version}] - {date.today().isoformat()}\n"
        f"{body}\n\n"
    )
    updated = text[: match.start()] + released + text[match.end() :].lstrip("\n")
    CHANGELOG.write_text(updated, encoding="utf-8")
    return body


def replace_marker(path: Path, replacement: str) -> None:
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        r"<!-- ARGUS_RELEASE_START -->.*?<!-- ARGUS_RELEASE_END -->",
        re.DOTALL,
    )
    if not pattern.search(text):
        return
    path.write_text(pattern.sub(replacement, text, count=1), encoding="utf-8")


def write_output(name: str, value: str) -> None:
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")
    else:
        print(f"{name}={value}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bump", choices=("major", "minor", "patch"), required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--pr-title", required=True)
    args = parser.parse_args()

    current = read_project_version()
    runtime = read_runtime_version()
    if runtime != current:
        raise SystemExit(
            f"version mismatch before release: pyproject={current}, argus.__version__={runtime}"
        )
    new_version = bump(current, args.bump)

    replace_version(
        PYPROJECT,
        r'^(version\s*=\s*)"[^"]+"\s*$',
        rf'\g<1>"{new_version}"',
    )
    replace_version(
        INIT,
        r'^(__version__\s*=\s*)"[^"]+"\s*$',
        rf'\g<1>"{new_version}"',
    )
    release_changelog(new_version, args.pr_number, args.pr_title)

    readme_marker = (
        "<!-- ARGUS_RELEASE_START -->\n"
        f"**Latest packaged release: v{new_version}** — "
        "[download desktop apps and installers](https://github.com/Varun-SV/Argus/releases/latest).\n"
        "<!-- ARGUS_RELEASE_END -->"
    )
    site_marker = (
        "<!-- ARGUS_RELEASE_START -->"
        f'<a class="release-pill" href="https://github.com/Varun-SV/Argus/releases/latest">'
        f"Latest release v{new_version}</a>"
        "<!-- ARGUS_RELEASE_END -->"
    )
    replace_marker(README, readme_marker)
    replace_marker(SITE, site_marker)

    write_output("current_version", current)
    write_output("version", new_version)
    write_output("tag", f"v{new_version}")


if __name__ == "__main__":
    main()
