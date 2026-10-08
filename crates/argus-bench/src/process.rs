//! Process data sources.
//!
//! The sampler reads the process table through [`ProcessSource`], so the tree walk and the
//! aggregation can be tested with a scripted fake (spec §13: unit tests inside each crate)
//! while the real harness uses [`SysinfoSource`] on every operating system.

use std::path::Path;

use sysinfo::{Pid, ProcessRefreshKind, ProcessesToUpdate, System, UpdateKind};

/// One process as seen in one snapshot of the process table.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ProcInfo {
    /// Operating-system process id.
    pub pid: u32,
    /// Parent process id, when the operating system reports one.
    pub parent: Option<u32>,
    /// Start time in seconds since the Unix epoch. Together with `pid` it identifies a process
    /// across samples, so that a reused PID is not mistaken for a tree member.
    pub start_time: u64,
    /// Short process name: the executable file name when readable, else the OS process name.
    /// Used for the per-name breakdown.
    pub name: String,
    /// Process name as the OS reports it (on Linux the 15-character `comm`, which for a Python
    /// console script is the script name, not `python`). Used, with `name`, by `attach --name`.
    pub os_name: String,
    /// Memory in bytes, using the per-OS metric named by [`memory_metric`].
    pub memory_bytes: u64,
}

impl ProcInfo {
    /// Identity of this process across samples.
    pub fn id(&self) -> ProcId {
        ProcId {
            pid: self.pid,
            start_time: self.start_time,
        }
    }
}

/// Identity of a process: PID plus start time (guards against PID reuse).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct ProcId {
    /// Operating-system process id.
    pub pid: u32,
    /// Start time in seconds since the Unix epoch.
    pub start_time: u64,
}

/// A source of process-table snapshots.
pub trait ProcessSource {
    /// Returns every process currently visible (threads excluded).
    fn snapshot(&mut self) -> Vec<ProcInfo>;

    /// Asks the operating system to terminate the given processes. Returns how many were
    /// signalled. Processes that already exited, or whose start time no longer matches, are
    /// skipped.
    fn terminate(&mut self, ids: &[ProcId]) -> usize;
}

/// The memory metric used on this operating system, recorded in every result.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MemoryMetric {
    /// Short machine-readable name: `rss` or `working_set`.
    pub name: &'static str,
    /// Human-readable definition, including where the number comes from.
    pub definition: &'static str,
}

/// Returns the memory metric that [`SysinfoSource`] reports on the current operating system.
pub fn memory_metric() -> MemoryMetric {
    if cfg!(target_os = "windows") {
        MemoryMetric {
            name: "working_set",
            definition: "Windows working set per process (GetProcessMemoryInfo WorkingSetSize), \
                         summed over the process tree; shared pages are counted once per process",
        }
    } else if cfg!(target_os = "macos") {
        MemoryMetric {
            name: "rss",
            definition: "macOS resident size per process (proc_pidinfo pti_resident_size), \
                         summed over the process tree; shared pages are counted once per process",
        }
    } else if cfg!(target_os = "linux") {
        MemoryMetric {
            name: "rss",
            definition: "Linux resident set size per process (/proc/<pid>/statm resident), \
                         summed over the process tree; shared pages are counted once per process",
        }
    } else {
        MemoryMetric {
            name: "rss",
            definition: "resident memory per process as reported by the sysinfo crate, summed \
                         over the process tree",
        }
    }
}

/// Real process source backed by the `sysinfo` crate (Linux, macOS and Windows).
pub struct SysinfoSource {
    system: System,
}

impl std::fmt::Debug for SysinfoSource {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("SysinfoSource").finish_non_exhaustive()
    }
}

impl Default for SysinfoSource {
    fn default() -> Self {
        Self::new()
    }
}

impl SysinfoSource {
    /// Creates a source with an empty process table; the first snapshot fills it.
    pub fn new() -> Self {
        Self {
            system: System::new(),
        }
    }

    fn refresh_kind() -> ProcessRefreshKind {
        // Threads are not separate processes: on Linux sysinfo would otherwise list every task
        // with the memory of its whole process, which would multiply the sum.
        ProcessRefreshKind::nothing()
            .without_tasks()
            .with_memory()
            .with_exe(UpdateKind::OnlyIfNotSet)
    }
}

fn display_name(process: &sysinfo::Process) -> String {
    process
        .exe()
        .and_then(Path::file_name)
        .map(|n| n.to_string_lossy().into_owned())
        .filter(|n| !n.is_empty())
        .unwrap_or_else(|| process.name().to_string_lossy().into_owned())
}

impl ProcessSource for SysinfoSource {
    fn snapshot(&mut self) -> Vec<ProcInfo> {
        self.system
            .refresh_processes_specifics(ProcessesToUpdate::All, true, Self::refresh_kind());
        self.system
            .processes()
            .values()
            .filter(|p| p.thread_kind().is_none())
            .map(|p| ProcInfo {
                pid: p.pid().as_u32(),
                parent: p.parent().map(Pid::as_u32),
                start_time: p.start_time(),
                name: display_name(p),
                os_name: p.name().to_string_lossy().into_owned(),
                memory_bytes: p.memory(),
            })
            .collect()
    }

    fn terminate(&mut self, ids: &[ProcId]) -> usize {
        let pids: Vec<Pid> = ids.iter().map(|id| Pid::from_u32(id.pid)).collect();
        self.system.refresh_processes_specifics(
            ProcessesToUpdate::Some(&pids),
            true,
            Self::refresh_kind(),
        );
        let mut signalled = 0;
        for id in ids {
            if let Some(process) = self.system.process(Pid::from_u32(id.pid)) {
                if process.start_time() == id.start_time && process.kill() {
                    signalled += 1;
                }
            }
        }
        signalled
    }
}
