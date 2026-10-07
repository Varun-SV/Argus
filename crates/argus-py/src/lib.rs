//! `argus._native`: the PyO3 extension module behind the documented Argus Python API
//! (docs/rearchitecture/specification.md §10, parity inventory §10).
//!
//! Phase P0 skeleton: it exposes only `version()`. The packaging work package owns this crate
//! and the maturin wheel build.
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

/// Version of the native core, returned to Python as `argus._native.version()`.
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
}
