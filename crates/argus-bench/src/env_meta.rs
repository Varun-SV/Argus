//! Environment metadata recorded in each result.
//!
//! Deliberately limited (work package BENCH, B4): OS family, OS name and version, CPU
//! architecture, logical CPU count, total RAM. No hostname, user name or machine paths.

use serde::{Deserialize, Serialize};
use sysinfo::{MemoryRefreshKind, RefreshKind, System};

/// Machine description stored in a result.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Environment {
    /// `linux`, `macos` or `windows` (Rust `std::env::consts::OS`).
    pub os: String,
    /// Operating-system or distribution name, e.g. `Ubuntu` or `Windows`, when known.
    pub os_name: Option<String>,
    /// Operating-system version, e.g. `24.04` or `11 (26100)`, when known.
    pub os_version: Option<String>,
    /// CPU architecture of the harness build (`x86_64`, `aarch64`).
    pub arch: String,
    /// Logical CPUs available to the harness.
    pub logical_cpus: u32,
    /// Total physical memory in bytes.
    pub total_ram_bytes: u64,
}

/// Collects the environment metadata of the current machine.
pub fn collect() -> Environment {
    let system = System::new_with_specifics(
        RefreshKind::nothing().with_memory(MemoryRefreshKind::nothing().with_ram()),
    );
    let logical_cpus = std::thread::available_parallelism()
        .map(|n| u32::try_from(n.get()).unwrap_or(u32::MAX))
        .unwrap_or(1);
    Environment {
        os: std::env::consts::OS.to_owned(),
        os_name: System::name(),
        os_version: System::os_version(),
        arch: std::env::consts::ARCH.to_owned(),
        logical_cpus,
        total_ram_bytes: system.total_memory(),
    }
}
