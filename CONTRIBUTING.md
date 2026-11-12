# Contributing to Argus

Thanks for contributing to Argus.

## Development setup

Argus requires Python 3.10 or newer.

```bash
python -m venv .venv
# Activate the environment for your shell, then:
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
pytest -q
```

Install platform or feature extras when the change needs them, for example:

```bash
python -m pip install -e ".[dev,windows]"
python -m pip install -e ".[dev,linux]"
python -m pip install -e ".[dev,browser,gui]"
```

## Pull requests

Keep each PR cohesive and preserve Argus' trust boundaries. In particular:

- `Adapter` describes **how** Argus interacts.
- `ExecutionEnvironment -> Capsule -> Adapter` describes **where** isolated execution occurs.
- Fleet sits above the existing Capsule boundary; it does not create a second Capsule abstraction.
- ATES is the evidence authority; reports and transport metadata must not silently rewrite canonical evidence.
- Never turn heartbeat loss alone into execution fencing authority.
- Avoid review-round scratch files and generated audit artifacts in the repository.

Add or update regression tests for behavior changes. CI runs across Windows, Linux, macOS, x64, and ARM64 where supported.

## Release labels

Every merged PR automatically produces a patch release unless it carries another release label:

- `bump:major`
- `bump:minor`
- `bump:patch`
- `release:none`

Use `release:none` for changes that should land on `main` without publishing a new user release.

## Security

Do not report sensitive vulnerabilities in ordinary public issues. Follow [SECURITY.md](SECURITY.md).

## Rust workspace (preview, phase P0)

The Rust re-architecture is built in `crates/` next to the Python package, which stays the
shipped product until the switch. The design is in
[docs/rearchitecture/specification.md](docs/rearchitecture/specification.md).

- **Toolchain:** stable Rust via `rust-toolchain.toml` (rustup installs it, with rustfmt and
  clippy, on first use). The minimum supported version is `rust-version` in `Cargo.toml`.
- **Preview binary:** the CLI builds as `argus-next` until the switch, so it never shadows the
  `argus` command from the Python package.
- **Before opening a PR**, run from the repository root:

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test --workspace
cargo build --release -p argus-cli && ./target/release/argus-next --version
# Dependency policy (install once: cargo install --locked cargo-deny cargo-audit)
cargo deny check
cargo audit
```

- Dependencies are declared once in `[workspace.dependencies]` with exact versions, and
  `Cargo.lock` is committed. New dependencies must pass `cargo deny check` (`deny.toml`).
- `unsafe` code is forbidden in every crate except the PyO3 boundary in `crates/argus-py`.
- The `Rust CI` workflow runs these checks on Linux, Windows and macOS, x64 and ARM64.
