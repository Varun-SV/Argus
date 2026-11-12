//! `argus_next._native`: the PyO3 extension module behind the documented Argus Python API
//! (docs/rearchitecture/specification.md §10, parity inventory §10).
//!
//! # Names during migration
//!
//! Until G-SWITCH (spec §12) the Python package `argus` from the `argus-app-testing`
//! distribution is the shipped product and must not be shadowed. This extension is therefore
//! packaged as `argus_next._native` in the preview distribution `argus-next` (see
//! `python/pyproject.toml`). At G-SWITCH the module becomes `argus._native` in
//! `argus-app-testing`; only the `module-name` in the maturin configuration and the Python
//! package directory change, not this crate. The `#[pymodule]` below is named `_native` for
//! that reason: the init symbol (`PyInit__native`) is the same under both package names.
//!
//! Phase P0 exposes only `version()`. The provisioning API listed in the parity inventory §10
//! is added in P8.
//!
//! # Unsafe code policy
//!
//! This is the PyO3 FFI boundary, the one crate in the workspace that does not use
//! `#![forbid(unsafe_code)]` (spec §14). The `#[pymodule]` and `#[pyfunction]` macros expand
//! to `unsafe` FFI glue that PyO3 audits; `forbid` would reject it. Hand-written `unsafe` is
//! still denied by the crate lint table (`unsafe_code = "deny"`) and needs an explicit,
//! reviewed `#[allow(unsafe_code)]` with a `// SAFETY:` comment. Unsafe operations inside
//! `unsafe fn` bodies must be wrapped in their own `unsafe` blocks.

#![deny(unsafe_op_in_unsafe_fn)]

use pyo3::prelude::*;

/// Version of the native core, returned to Python as `argus_next._native.version()`.
///
/// It is the workspace version, which is also the version of the Python distribution
/// (maturin reads it from `Cargo.toml`), so `argus_next.native_version()` and
/// `importlib.metadata.version("argus-next")` agree.
#[must_use]
pub fn native_version() -> &'static str {
    argus_core::PREVIEW_VERSION
}

/// Return the version of the native Argus core.
#[pyfunction]
fn version() -> &'static str {
    native_version()
}

/// The `_native` extension module.
#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(version, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    #[test]
    fn native_version_is_core_version() {
        assert_eq!(super::native_version(), argus_core::PREVIEW_VERSION);
    }

    #[test]
    fn native_version_is_workspace_version() {
        // argus-py inherits `version.workspace = true`, so its package version is the
        // workspace version that maturin writes into the wheel metadata.
        assert_eq!(super::native_version(), env!("CARGO_PKG_VERSION"));
    }
}
