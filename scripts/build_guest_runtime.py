"""Freeze the guest driver and installed adapter dependencies into an offline ZIP.

Run on the target OS/architecture, using an operator-approved dependency
environment. This command never installs packages or downloads browser engines.
Linux builds must use a distribution ABI compatible with the intended guest.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--runtime-version", required=True)
    args = parser.parse_args(argv)
    system = platform.system().lower()
    if system not in {"windows", "linux"} or platform.machine().lower() not in {"amd64", "x86_64"}:
        parser.error("guest runtime builds require a native Windows/Linux x86_64 host")
    if args.output.exists():
        parser.error("output already exists")
    from PyInstaller.utils.hooks import collect_submodules
    from argus.provisioning.runtime_bundle import create_guest_runtime_bundle
    target_os = "windows-11" if system == "windows" else "ubuntu"

    with tempfile.TemporaryDirectory(prefix="argus-runtime-build-") as directory:
        root = Path(directory)
        driver = root / "driver.py"
        driver.write_text("from argus.capsule.runtime_entrypoint import main\n"
                          "if __name__ == '__main__':\n    main()\n", encoding="utf-8")
        command = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
                   "--name", "argus-guest", "--distpath", str(root / "dist"),
                   "--workpath", str(root / "build"), "--specpath", str(root),
                   "--paths", str(Path(__file__).resolve().parents[1])]
        modules = collect_submodules("argus.capsule") + collect_submodules("argus.adapters")
        if system == "windows":
            modules += ["servicemanager", "win32service", "win32serviceutil", "win32net",
                        "win32netcon", "win32security", "win32ts", "win32profile", "win32job",
                        "win32process", "win32pipe", "win32event", "win32api", "win32con"]
        for module in sorted(set(modules)):
            command += ["--hidden-import", module]
        command.append(str(driver))
        subprocess.run(command, check=True)
        entrypoint = "argus-guest.exe" if system == "windows" else "argus-guest"
        result = create_guest_runtime_bundle(
            root / "dist" / "argus-guest", args.output,
            runtime_version=args.runtime_version, target_os=target_os,
            target_architecture="x86_64", entrypoint=entrypoint,
        )
        print(json.dumps({
            "bundle_path": str(result.path.resolve()),
            "runtime_bundle_sha256": result.sha256,
            "runtime_version": args.runtime_version,
            "target_os": target_os,
            "target_architecture": "x86_64",
            "bundle_format_version": result.manifest.format_version,
            "bootstrap_schema_version": result.manifest.bootstrap_schema_version,
            "bootstrap_service_policy_version": result.manifest.bootstrap_service_policy_version,
            "runtime_installation_policy_version": result.manifest.installation_policy_version,
        }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
