# P0 packaging findings: one wheel with a native binary and a Python extension

Phase P0 of the [migration plan](specification.md#12-migration-plan) asks for "maturin
wheels". [Specification §10](specification.md#10-python-packaging-and-api) requires that
`pip install argus-app-testing` keeps installing the `argus` command plus a Python API, with the
native binaries in platform wheels built by maturin. This page records how that works in the P0
prototype, `argus-next`, and what was verified.

## The problem

maturin builds a wheel from **one** crate with **one** kind of bindings: either a PyO3 extension
(`bindings = "pyo3"`) or Rust binaries (`bindings = "bin"`), not both. The distribution needs
both:

- the native command (`argus-next` now, `argus` and `argus-gui` at G-SWITCH) on `PATH`;
- the Python API backed by a native extension (`argus_next._native` now, the documented `argus`
  API at G-SWITCH).

## The layout that works

| Part | Built from | How it gets into the wheel |
|---|---|---|
| `argus_next/_native.*` (abi3, CPython 3.10+) | `crates/argus-py`, `bindings = "pyo3"` | maturin, as the wheel's extension |
| `argus_next/` (Python package, typing stub) | `python/argus_next/` | maturin `python-source = "."` |
| `argus_next-<ver>.data/scripts/argus-next[.exe]` | `crates/argus-cli`, built first | staged into `python/argus_next.data/scripts/`; maturin packs the `data` directory, and installers put `scripts/` next to the interpreter's console scripts, so the command is on `PATH` |

`python/build_backend/argus_next_build.py` is a thin PEP 517 backend around maturin's:

- `build_wheel` and `build_editable` build and stage the CLI binary for the host first
  (or, with `ARGUS_NEXT_CLI_PRESTAGED=1`, require a binary staged beforehand);
- the metadata hooks and `build_sdist` create the empty `argus_next.data/scripts/` directory,
  because maturin refuses to run, even for metadata or an sdist, when the configured `data`
  directory is missing, and that directory is gitignored;
- `build_sdist` adds `crates/argus-cli` to the sdist, which maturin leaves out because it is not
  a path dependency of `argus-py`;
- `python build_backend/argus_next_build.py stage-cli` is the command-line entry CI uses:
  `--target`, `--zig-glibc 2.28` (via `cargo zigbuild`) and `--universal2` (two macOS builds
  joined with `lipo`).

The preview uses its own distribution, import package and command (`argus-next`,
`argus_next`, `argus-next`), so it installs next to `argus-app-testing` without conflicts. At
G-SWITCH the same layout carries `argus` and `argus-gui` under the `argus-app-testing`
distribution.

## Commands

```sh
# Host build from the source tree (PEP 517):
pip wheel ./python -w dist

# What CI does (.github/workflows/rust-wheels.yml), here for Linux x86_64:
cd python
python build_backend/argus_next_build.py stage-cli --locked \
    --target x86_64-unknown-linux-gnu --zig-glibc 2.28
ARGUS_NEXT_CLI_PRESTAGED=1 maturin build --release --locked \
    --zig --compatibility manylinux_2_28 --target x86_64-unknown-linux-gnu --out ../dist
```

CI builds Linux x64 and ARM64 (`manylinux_2_28`, with zig), Windows x64 and ARM64, and macOS
universal2. It installs every wheel into a clean virtual environment and runs `argus-next
--version`, `import argus_next; argus_next.native_version()` and `python -m argus_next
--version` outside the source tree. Tool versions are pinned in the workflow: maturin 1.16.0,
ziglang 0.17.1 and cargo-zigbuild 0.24.1.

## Verified

On a Linux x86_64 host (Python 3.13, Rust stable):

| Check | Result |
|---|---|
| `pip wheel ./python` from a clean tree (no staged directory) | builds `argus_next-0.0.0-cp310-abi3-linux_x86_64.whl` |
| Wheel contents | `argus_next/__init__.py`, `__main__.py`, `_native.abi3.so`, `_native.pyi`, `py.typed`, `argus_next-0.0.0.data/scripts/argus-next`, dist-info with a CycloneDX SBOM |
| The CI path (`stage-cli --zig-glibc 2.28`, `maturin build --zig --compatibility manylinux_2_28`) | builds `argus_next-0.0.0-cp310-abi3-manylinux_2_28_x86_64.whl` |
| glibc symbols the staged binary needs (`objdump -T`) | at most `GLIBC_2.28`, so the binary is also manylinux_2_28 (maturin's own audit covers only the extension) |
| Install into a clean venv; `argus-next --version`, `native_version()`, `python -m argus_next --version` | `argus-next 0.0.0`, `0.0.0`, `argus-next 0.0.0` |
| `python/tests` (launcher tests) | 9 passed |

Not verified here: the Windows and macOS wheels and the Linux ARM64 wheel, which are built and
smoke-tested only by the CI workflow. Code signing and notarization of the binaries are out of
scope for P0 (they follow the release workflow's existing signing status).
