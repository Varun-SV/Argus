#!/usr/bin/env python3
"""Generate a deterministic WiX v4 source file for the staged Argus app.

WiX v4 does not provide the newer declarative Files harvesting syntax used by
later WiX releases. Rather than accepting a newer toolchain EULA in CI or
checking generated file lists into source control, this generator converts the
exact staged release tree into ordinary Directory/Component/File authoring.

Component IDs and GUIDs are derived solely from the case-insensitive installed
relative path, so unchanged files retain component identity across upgrades.
"""

from __future__ import annotations

import argparse
import hashlib
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

WIX_NS = "http://wixtoolset.org/schemas/v4/wxs"
UPGRADE_CODE = "3F5E6BD9-95D5-5E4F-9C3A-DF93D4314D07"
COMPONENT_NAMESPACE = uuid.UUID("25b76661-a9d8-54e4-b44a-f10ab262cc77")
GUI_RELATIVE_PATH = PurePosixPath("Argus/Argus.exe")

ET.register_namespace("", WIX_NS)


def _tag(name: str) -> str:
    return f"{{{WIX_NS}}}{name}"


def _stable_suffix(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


def _component_guid(relative_path: PurePosixPath) -> str:
    logical = relative_path.as_posix().casefold()
    return str(uuid.uuid5(COMPONENT_NAMESPACE, logical)).upper()


def _source_expression(relative_path: PurePosixPath) -> str:
    windows_path = str(relative_path).replace("/", "\\")
    return rf"$(SourceDir)\{windows_path}"


def _collect_files(source_dir: Path) -> list[tuple[PurePosixPath, Path]]:
    if not source_dir.is_dir():
        raise ValueError(f"source directory does not exist: {source_dir}")

    collected: list[tuple[PurePosixPath, Path]] = []
    seen_casefold: dict[str, PurePosixPath] = {}
    for path in sorted(
        (item for item in source_dir.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(source_dir).as_posix().casefold(),
    ):
        relative = PurePosixPath(path.relative_to(source_dir).as_posix())
        folded = relative.as_posix().casefold()
        prior = seen_casefold.get(folded)
        if prior is not None and prior != relative:
            raise ValueError(
                "staged release contains Windows case-colliding paths: "
                f"{prior.as_posix()} and {relative.as_posix()}"
            )
        seen_casefold[folded] = relative
        collected.append((relative, path))

    if not collected:
        raise ValueError("staged release contains no files")
    if GUI_RELATIVE_PATH.as_posix().casefold() not in seen_casefold:
        raise ValueError(
            f"staged release is missing GUI executable {GUI_RELATIVE_PATH.as_posix()}"
        )
    return collected


def build_tree(source_dir: Path, *, version: str) -> ET.ElementTree:
    files = _collect_files(source_dir)

    wix = ET.Element(_tag("Wix"))
    package = ET.SubElement(
        wix,
        _tag("Package"),
        {
            "Name": "Argus",
            "Manufacturer": "Varun S V",
            "Version": version,
            "UpgradeCode": UPGRADE_CODE,
            "Language": "1033",
            "Scope": "perMachine",
        },
    )
    ET.SubElement(
        package,
        _tag("MajorUpgrade"),
        {"DowngradeErrorMessage": "A newer version of Argus is already installed."},
    )
    ET.SubElement(package, _tag("MediaTemplate"), {"EmbedCab": "yes"})

    program_files = ET.SubElement(
        package, _tag("StandardDirectory"), {"Id": "ProgramFiles6432Folder"}
    )
    install_folder = ET.SubElement(
        program_files, _tag("Directory"), {"Id": "INSTALLFOLDER", "Name": "Argus"}
    )
    ET.SubElement(package, _tag("StandardDirectory"), {"Id": "ProgramMenuFolder"})

    directories: dict[PurePosixPath, ET.Element] = {
        PurePosixPath("."): install_folder
    }

    def directory_for(relative_parent: PurePosixPath) -> ET.Element:
        if relative_parent in directories:
            return directories[relative_parent]
        parent = directory_for(relative_parent.parent)
        current = ET.SubElement(
            parent,
            _tag("Directory"),
            {
                "Id": f"Dir_{_stable_suffix(relative_parent.as_posix().casefold())}",
                "Name": relative_parent.name,
            },
        )
        directories[relative_parent] = current
        return current

    component_ids: list[str] = []
    for relative, _ in files:
        parent = directory_for(relative.parent)
        suffix = _stable_suffix(relative.as_posix().casefold())
        component_id = f"Cmp_{suffix}"
        file_id = f"File_{suffix}"
        component = ET.SubElement(
            parent,
            _tag("Component"),
            {"Id": component_id, "Guid": _component_guid(relative)},
        )
        file_element = ET.SubElement(
            component,
            _tag("File"),
            {
                "Id": file_id,
                "Source": _source_expression(relative),
                "KeyPath": "yes",
            },
        )
        if relative.as_posix().casefold() == GUI_RELATIVE_PATH.as_posix().casefold():
            ET.SubElement(
                file_element,
                _tag("Shortcut"),
                {
                    "Id": "ArgusStartMenuShortcut",
                    "Directory": "ProgramMenuFolder",
                    "Name": "Argus",
                    "Description": "Argus autonomous application testing",
                    "WorkingDirectory": "INSTALLFOLDER",
                },
            )
        component_ids.append(component_id)

    feature = ET.SubElement(
        package,
        _tag("Feature"),
        {"Id": "MainFeature", "Title": "Argus", "Level": "1"},
    )
    for component_id in component_ids:
        ET.SubElement(feature, _tag("ComponentRef"), {"Id": component_id})

    ET.indent(wix, space="  ")
    return ET.ElementTree(wix)


def write_wix_source(source_dir: Path, output: Path, *, version: str) -> None:
    tree = build_tree(source_dir, version=version)
    output.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output, encoding="utf-8", xml_declaration=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    write_wix_source(args.source_dir, args.output, version=args.version)


if __name__ == "__main__":
    main()
