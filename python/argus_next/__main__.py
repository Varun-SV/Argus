"""``python -m argus_next``: run the native ``argus-next`` binary installed with this package.

The wheel installs the binary into the interpreter's scripts directory (``bin`` or ``Scripts``),
next to the Python console scripts. This module finds it there and runs it with the same
arguments. The exit code is the binary's exit code (spec section 10: ``python -m`` keeps working
by executing the binary).

``PATH`` is deliberately not searched: another ``argus-next`` on ``PATH`` may belong to a
different installation and version. This module imports nothing from the package so that it also
works when the extension module cannot be loaded.
"""

from __future__ import annotations

import os
import subprocess
import sys
import sysconfig
from pathlib import Path

BINARY_STEM = "argus-next"


def binary_name() -> str:
    """Return the file name of the native binary on this platform."""
    suffix = ".exe" if os.name == "nt" else ""
    return BINARY_STEM + suffix


def _candidate_dirs() -> list[Path]:
    """Directories where an installer puts the wheel's scripts, most specific first."""
    dirs = [Path(sysconfig.get_path("scripts"))]
    try:
        user_scheme = sysconfig.get_preferred_scheme("user")
        dirs.append(Path(sysconfig.get_path("scripts", user_scheme)))
    except (KeyError, ValueError):  # pragma: no cover - interpreter without a user scheme
        pass
    # ``pip install --target DIR`` puts packages in DIR and scripts in DIR/bin (DIR/Scripts).
    package_root = Path(__file__).resolve().parent.parent
    dirs.append(package_root / ("Scripts" if os.name == "nt" else "bin"))
    unique: list[Path] = []
    for directory in dirs:
        if directory not in unique:
            unique.append(directory)
    return unique


def find_argus_next_bin() -> str:
    """Return the path of the installed ``argus-next`` binary.

    Raises:
        FileNotFoundError: no executable ``argus-next`` in any candidate directory. The message
            lists every path that was checked.
    """
    name = binary_name()
    checked: list[str] = []
    for directory in _candidate_dirs():
        candidate = directory / name
        checked.append(str(candidate))
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise FileNotFoundError(
        f"the {BINARY_STEM} binary was not found; checked: " + ", ".join(checked)
    )


def _run_child(binary: str, args: list[str]) -> int:
    """Run the binary as a child process and return its exit code (used on Windows)."""
    process = subprocess.Popen([binary, *args])
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            # The console delivers Ctrl+C to the child too; wait for it to finish so that its
            # exit code is the one reported.
            continue


def main(argv: list[str] | None = None) -> int:
    """Run ``argus-next`` with ``argv`` (default: this process's arguments)."""
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        binary = find_argus_next_bin()
    except FileNotFoundError as exc:
        print(f"python -m argus_next: {exc}", file=sys.stderr)
        return 1
    if os.name == "nt":
        return _run_child(binary, args)
    # Replace this interpreter so signals, the exit code and the terminal belong to the binary.
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(binary, [binary, *args])
    return 1  # pragma: no cover - execv does not return


if __name__ == "__main__":
    sys.exit(main())
