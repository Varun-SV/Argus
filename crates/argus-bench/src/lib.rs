//! `argus-bench`: benchmark harness for the performance budgets in
//! docs/rearchitecture/specification.md §11 (gate G-PERF).
//!
//! The harness samples whole-process-tree memory at a fixed interval (50 ms by default) and
//! reports the median of N runs (5 by default). Two modes exist:
//!
//! * `run`: spawn the argv of a scenario file (`bench/scenarios/*.toml`) and measure it;
//! * `attach`: sample an already running process tree for a fixed time.
//!
//! Process data comes from the [`process::ProcessSource`] trait; the real implementation uses
//! the `sysinfo` crate on Linux, macOS and Windows, tests use a scripted fake.
//! See bench/README.md and docs/rearchitecture/benchmarks.md.

#![forbid(unsafe_code)]

pub mod attach;
pub mod env_meta;
pub mod process;
pub mod report;
pub mod runner;
pub mod sampler;
pub mod scenario;
pub mod stats;
pub mod timestamp;
pub mod tree;

/// Version of this tool, recorded in every result.
pub const TOOL_VERSION: &str = env!("CARGO_PKG_VERSION");

/// Default sampling interval (spec §11: "samples whole-process-tree RSS at 50 ms").
pub const DEFAULT_INTERVAL_MS: u64 = 50;

/// Default number of counted runs (spec §11: "reports median of 5 runs").
pub const DEFAULT_RUNS: u32 = 5;
