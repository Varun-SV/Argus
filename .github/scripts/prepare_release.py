#!/usr/bin/env python3
"""Parse CHANGELOG.md, bump pyproject.toml version, emit release info to GITHUB_OUTPUT."""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent


def read_version(pyproject: Path) -> tuple[int, int, int]:
    text = pyproject.read_text()
    m = re.search(r'^version\s*=\s*"(\d+)\.(\d+)\.(\d+)"', text, re.MULTILINE)
    if not m:
        sys.exit("Could not find version in pyproject.toml")
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def bump_version(major: int, minor: int, patch: int, bump: str) -> tuple[int, int, int]:
    if bump == "major":
        return major + 1, 0, 0
    if bump == "minor":
        return major, minor + 1, 0
    return major, minor, patch + 1


def extract_unreleased(changelog: Path) -> str:
    text = changelog.read_text()
    m = re.search(
        r"^## \[Unreleased\]\n(.*?)(?=^## \[|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not m:
        return ""
    return m.group(1).strip()


def update_changelog(changelog: Path, version: str, notes: str) -> None:
    text = changelog.read_text()
    today = date.today().isoformat()
    new_header = f"## [{version}] - {today}"
    text = text.replace("## [Unreleased]", f"## [Unreleased]\n\n{new_header}", 1)
    # Remove the body that was under [Unreleased] (now moved under the version header)
    # Actually we want: keep [Unreleased] empty, add versioned block with the notes
    text = re.sub(
        r"(## \[Unreleased\]\n)(.*?)(?=## \[)",
        rf"\1\n{new_header}\n{notes}\n\n",
        text,
        count=1,
        flags=re.DOTALL,
    )
    changelog.write_text(text)


def update_pyproject(pyproject: Path, version: str) -> None:
    text = pyproject.read_text()
    text = re.sub(
        r'^(version\s*=\s*)"[\d.]+"',
        rf'\1"{version}"',
        text,
        count=1,
        flags=re.MULTILINE,
    )
    pyproject.write_text(text)


def write_output(key: str, value: str) -> None:
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a") as f:
            # Multi-line values use heredoc syntax
            if "\n" in value:
                delimiter = "EOF_RELEASE_NOTES"
                f.write(f"{key}<<{delimiter}\n{value}\n{delimiter}\n")
            else:
                f.write(f"{key}={value}\n")
    else:
        print(f"{key}={value!r}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bump", choices=["major", "minor", "patch"], default="patch")
    args = parser.parse_args()

    pyproject = ROOT / "pyproject.toml"
    changelog = ROOT / "CHANGELOG.md"

    notes = extract_unreleased(changelog)
    if not notes:
        print("No [Unreleased] content found — skipping release.", file=sys.stderr)
        return

    major, minor, patch = read_version(pyproject)
    new_major, new_minor, new_patch = bump_version(major, minor, patch, args.bump)
    version = f"{new_major}.{new_minor}.{new_patch}"

    update_changelog(changelog, version, notes)
    update_pyproject(pyproject, version)

    write_output("version", version)
    write_output("notes", notes)

    print(f"Prepared release v{version}", file=sys.stderr)


if __name__ == "__main__":
    main()
