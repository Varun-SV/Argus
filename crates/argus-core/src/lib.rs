//! Argus core library.
//!
//! Phase P0 skeleton of the Rust re-architecture (docs/rearchitecture/specification.md §4).
//! This crate will hold configuration, test-spec parsing, actions and policy, secrets,
//! budgets, tokens and run history (phase P1, gate G-CORE). For now it only exposes the
//! preview version and the registry of on-disk format identifiers.

#![forbid(unsafe_code)]

/// Version of the Rust preview build, taken from the workspace package version.
///
/// The preview binary is `argus-next` until G-SWITCH (spec §12); the Python release line keeps
/// its own version until then.
pub const PREVIEW_VERSION: &str = env!("CARGO_PKG_VERSION");

/// Name of the preview command-line binary (spec §12: `argus-next` until G-SWITCH).
pub const PREVIEW_BINARY_NAME: &str = "argus-next";

/// A versioned on-disk or on-wire format whose identifier must stay byte-identical with the
/// Python implementation.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FormatVersion {
    /// Compatibility contract from spec §5 that governs this format (for example `C-03`).
    pub contract: &'static str,
    /// Exact identifier string as written by the Python implementation.
    pub identifier: &'static str,
}

/// Registry of compatibility-critical format identifiers.
///
/// Intentionally empty in P0. The identifiers governed by C-03 (ATES evidence and manifest
/// version strings) are defined in `argus-ates` (phase P2, gate G-ATES), and the
/// `argus-fleet-*-v1` identifiers governed by C-04 are defined in `argus-fleet` (phase P7).
/// Those crates own the constants and test them against the golden fixtures in
/// `tests/golden/`; they are not duplicated here so that each identifier has one source of
/// truth.
pub const FORMAT_VERSIONS: &[FormatVersion] = &[];

/// Human-readable banner printed by the preview binaries.
#[must_use]
pub fn preview_banner() -> String {
    format!(
        "{PREVIEW_BINARY_NAME} {PREVIEW_VERSION} (Rust preview; the Python `argus` command \
         remains the supported release until the switch)"
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn preview_version_matches_package_version() {
        assert_eq!(PREVIEW_VERSION, env!("CARGO_PKG_VERSION"));
        assert!(!PREVIEW_VERSION.is_empty());
        assert_eq!(PREVIEW_VERSION.split('.').count(), 3, "expected semver x.y.z");
    }

    #[test]
    fn banner_names_preview_binary_and_version() {
        let banner = preview_banner();
        assert!(banner.starts_with("argus-next "));
        assert!(banner.contains(PREVIEW_VERSION));
        assert!(banner.contains("preview"));
    }

    #[test]
    fn format_registry_is_empty_until_owning_crates_exist() {
        // C-03/C-04 identifiers live in argus-ates and argus-fleet (later phases).
        assert!(FORMAT_VERSIONS.is_empty());
    }
}
