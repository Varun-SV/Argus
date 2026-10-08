# argus-next (preview)

Preview build of the Argus Rust re-architecture. It is **not** a replacement for the `argus`
command yet: keep using the `argus-app-testing` distribution for real work.

One platform wheel installs:

- `argus-next`, the native command-line binary, on `PATH`;
- `argus_next`, a Python module backed by a native extension (`argus_next._native`).

It installs next to `argus-app-testing` without conflicts (different distribution, import package
and command names).

```console
$ argus-next --version
$ python -c "import argus_next; print(argus_next.native_version())"
$ python -m argus_next --version
```

Building from source needs a Rust toolchain (see `rust-version` in the workspace `Cargo.toml`).

Design and verification: `docs/rearchitecture/p0-packaging-findings.md` in the source
repository.
