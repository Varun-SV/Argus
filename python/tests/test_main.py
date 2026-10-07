"""Tests for ``python -m argus_next`` (python/argus_next/__main__.py).

``__main__.py`` is loaded as a standalone module so these tests run from the source tree
without the compiled extension. The end-to-end check against an installed wheel is
python/tools/smoke_wheel.py.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

MAIN_PATH = Path(__file__).resolve().parents[1] / "argus_next" / "__main__.py"


def _load_main():
    spec = importlib.util.spec_from_file_location("argus_next_main_under_test", MAIN_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_binary(directory: Path, name: str, exit_code: int) -> Path:
    """Write a stand-in for the native binary that echoes its argv and exits with a code."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    script.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import sys
            print("ARGV=" + "|".join(sys.argv[1:]))
            sys.exit({exit_code})
            """
        ),
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def test_binary_name_has_platform_suffix():
    main = _load_main()
    expected = "argus-next.exe" if os.name == "nt" else "argus-next"
    assert main.binary_name() == expected


def test_finds_binary_in_interpreter_scripts_dir(tmp_path, monkeypatch):
    main = _load_main()
    scripts = tmp_path / "scripts"
    binary = _fake_binary(scripts, main.binary_name(), 0)
    monkeypatch.setattr(main, "_candidate_dirs", lambda: [tmp_path / "missing", scripts])
    assert main.find_argus_next_bin() == str(binary)


def test_scripts_dir_is_first_candidate():
    import sysconfig

    main = _load_main()
    assert main._candidate_dirs()[0] == Path(sysconfig.get_path("scripts"))


def test_target_install_bin_dir_is_a_candidate():
    main = _load_main()
    package_root = MAIN_PATH.resolve().parent.parent
    sub = "Scripts" if os.name == "nt" else "bin"
    assert package_root / sub in main._candidate_dirs()


def test_missing_binary_reports_searched_paths(tmp_path, monkeypatch, capsys):
    main = _load_main()
    monkeypatch.setattr(main, "_candidate_dirs", lambda: [tmp_path / "a", tmp_path / "b"])
    with pytest.raises(FileNotFoundError) as excinfo:
        main.find_argus_next_bin()
    message = str(excinfo.value)
    assert str(tmp_path / "a") in message and str(tmp_path / "b") in message
    assert main.main(["--version"]) == 1
    assert "argus-next" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="POSIX exec path")
def test_main_forwards_argv_and_exit_code_posix(tmp_path):
    """Run ``__main__.py`` in a child interpreter so ``os.execv`` replaces that child only."""
    scripts = tmp_path / "scripts"
    _fake_binary(scripts, "argus-next", 7)
    runner = textwrap.dedent(
        f"""\
        import importlib.util, sys
        from pathlib import Path
        spec = importlib.util.spec_from_file_location("m", {str(MAIN_PATH)!r})
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        m._candidate_dirs = lambda: [Path({str(scripts)!r})]
        sys.exit(m.main(sys.argv[1:]))
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", runner, "--version", "two words"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 7, result.stderr
    assert result.stdout.strip() == "ARGV=--version|two words"


def test_main_forwards_exit_code_via_subprocess(tmp_path, monkeypatch, capfd):
    """The Windows path (child process, exit code returned) is exercised on every OS."""
    main = _load_main()
    scripts = tmp_path / "scripts"
    _fake_binary(scripts, main.binary_name(), 3)
    monkeypatch.setattr(main, "_candidate_dirs", lambda: [scripts])
    if os.name == "nt":
        # A .exe cannot be faked with a script; this path is covered by smoke_wheel.py.
        pytest.skip("covered by the wheel smoke test on Windows")
    assert main._run_child(main.find_argus_next_bin(), ["a", "b"]) == 3
    assert capfd.readouterr().out.strip() == "ARGV=a|b"
