//! Argus protocol types.
//!
//! This crate will be the single source of truth for the serde types of every UI and API
//! request, response and event shared by the desktop app, `argus serve` and the CLI
//! (docs/rearchitecture/specification.md §4). The types are added together with the bridge
//! methods and intents they serve (phase P5, gate G-UI; parity inventory §11).
//!
//! Phase P0 deliberately defines no types: inventing message shapes before the parity work
//! would create a second, unreviewed contract.

#![forbid(unsafe_code)]

/// Version of this crate, for diagnostics.
pub const CRATE_VERSION: &str = env!("CARGO_PKG_VERSION");

#[cfg(test)]
mod tests {
    #[test]
    fn crate_version_is_set() {
        assert!(!super::CRATE_VERSION.is_empty());
    }
}
