//! Result document (`argus-bench-result-v1`) and the human-readable table.

use std::collections::{BTreeMap, BTreeSet};
use std::fmt::Write as _;

use serde::{Deserialize, Serialize};

use crate::env_meta::Environment;
use crate::stats::{Summary, summarize};
use crate::tree::RunMemory;

/// Schema identifier of a benchmark result document.
pub const RESULT_SCHEMA: &str = "argus-bench-result-v1";

/// Known measurement limits, copied into every result so a reader of the JSON sees them.
pub const KNOWN_LIMITS: &[&str] = &[
    "Processes that start and exit between two samples are never seen, and memory peaks \
     shorter than the sampling interval can be missed.",
    "Memory is summed per process: pages shared between processes (libraries, shared memory) \
     are counted once per process, so the sum can exceed the physical memory in use.",
    "A process that leaves the tree (is re-parented) before its first sample is not counted; \
     once seen, a process stays counted while it lives.",
    "Wall time runs from just before the root process is spawned until it exits, including \
     interpreter or runtime start-up; setup and teardown are excluded.",
];

/// Name and version of the tool that produced the result.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Tool {
    /// Always `argus-bench`.
    pub name: String,
    /// Crate version.
    pub version: String,
}

/// What was measured and how.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Metric {
    /// `rss` (Linux, macOS) or `working_set` (Windows).
    pub name: String,
    /// Definition of the per-process number and the aggregation.
    pub definition: String,
    /// Sampling interval in milliseconds.
    pub interval_ms: u64,
    /// How samples and runs are combined.
    pub aggregation: String,
}

/// Result status.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Status {
    /// Every counted run met its expectation.
    Ok,
    /// At least one run failed (unexpected exit, timeout, early exit).
    Failed,
    /// The scenario's `skip_unless` probe failed on this machine.
    Skipped,
    /// The scenario is a placeholder (`pending`) and was not run.
    Pending,
}

/// Measurement mode.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    /// `argus-bench run`: the harness spawns the scenario command.
    Run,
    /// `argus-bench attach`: the harness samples an already running tree.
    Attach,
}

/// Scenario details copied into the result (templates, never expanded machine paths).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ScenarioInfo {
    /// Scenario name.
    pub name: String,
    /// Scenario description.
    pub description: String,
    /// Scenario tags.
    pub tags: Vec<String>,
    /// Repository-relative scenario file, `/`-separated, when known.
    pub file: Option<String>,
    /// argv template of the measured command.
    pub command: Vec<String>,
    /// Uncounted warm-up runs performed.
    pub warmup: u32,
    /// Expected exit code (not checked when `duration_ms` is set).
    pub expected_exit: i32,
    /// Fixed measurement window per run, if the scenario stops the app itself.
    pub duration_ms: Option<u64>,
    /// Start-up excluded from peaks, in milliseconds.
    pub measure_after_ms: u64,
    /// Placeholder note when the scenario is pending.
    pub pending: Option<String>,
}

/// Attach-mode details.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AttachInfo {
    /// `pid` or `name`.
    pub selector: String,
    /// Process name given with `--name`.
    pub name: Option<String>,
    /// Number of root processes found.
    pub roots: u32,
    /// Requested sampling window in milliseconds.
    pub duration_ms: u64,
}

/// One run.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RunRecord {
    /// 1-based index, warm-up runs included.
    pub index: u32,
    /// Warm-up runs are not counted in the summary.
    pub warmup: bool,
    /// Exit code of the root process, when it exited with one.
    pub exit_code: Option<i32>,
    /// Signal that ended the root process (Unix), when any.
    pub signal: Option<i32>,
    /// The scenario's `duration` elapsed and the harness stopped the tree (expected for apps).
    pub stopped_after_duration: bool,
    /// The run hit the scenario timeout and the harness terminated the tree.
    pub timed_out: bool,
    /// The run met its expectation.
    pub ok: bool,
    /// Wall time in microseconds.
    pub wall_us: u64,
    /// Memory measurement.
    pub memory: RunMemory,
    /// Tree members still alive when the root exited (terminated by the harness in run mode).
    pub leftover_processes: u32,
}

/// Summary of one process name across counted runs.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NameSummary {
    /// Process name.
    pub name: String,
    /// Per-run peaks (0 for runs where the name did not appear), summarized.
    pub peak_bytes: Summary,
    /// Counted runs in which the name appeared.
    pub runs_seen: u32,
    /// Most processes with this name alive at once, over all counted runs.
    pub max_concurrent: u32,
}

/// Summary over the counted runs.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct RunSummary {
    /// Counted runs (warm-up excluded, failed runs excluded).
    pub runs: u32,
    /// Wall time in microseconds.
    pub wall_us: Summary,
    /// Peak tree memory in bytes.
    pub peak_tree_bytes: Summary,
    /// Mean tree memory in bytes.
    pub mean_tree_bytes: Summary,
    /// Distinct processes per run.
    pub distinct_processes: Summary,
    /// Per-name breakdown, largest median peak first.
    pub by_name: Vec<NameSummary>,
}

/// Comparison with the scenario's spec §11 target (medians against bounds).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TargetCheck {
    /// Source of the target.
    pub source: Option<String>,
    /// Wall-time bound in milliseconds.
    pub wall_ms_max: Option<f64>,
    /// Whether the median wall time is within the bound.
    pub wall_within: Option<bool>,
    /// Peak-memory bound in MiB.
    pub peak_tree_mib_max: Option<f64>,
    /// Whether the median peak tree memory is within the bound.
    pub peak_within: Option<bool>,
}

/// A complete result document.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BenchResult {
    /// Always [`RESULT_SCHEMA`].
    pub schema: String,
    /// Producing tool.
    pub tool: Tool,
    /// UTC timestamp (RFC 3339, seconds), from `--timestamp` or the clock.
    pub created_utc: String,
    /// Machine description.
    pub environment: Environment,
    /// Metric definition.
    pub metric: Metric,
    /// Measurement mode.
    pub mode: Mode,
    /// Overall status.
    pub status: Status,
    /// Why the status is not `ok`, or other notes.
    pub status_reason: Option<String>,
    /// Free-form labels given with `--label key=value`.
    pub labels: BTreeMap<String, String>,
    /// Scenario details (run mode).
    pub scenario: Option<ScenarioInfo>,
    /// Attach details (attach mode).
    pub attach: Option<AttachInfo>,
    /// Every run, warm-up included.
    pub runs: Vec<RunRecord>,
    /// Summary over counted runs.
    pub summary: Option<RunSummary>,
    /// Target comparison.
    pub target: Option<TargetCheck>,
    /// Known measurement limits.
    pub known_limits: Vec<String>,
}

/// Summarizes the counted runs: not warm-up, and `ok`.
pub fn summarize_runs(runs: &[RunRecord]) -> Option<RunSummary> {
    let counted: Vec<&RunRecord> = runs.iter().filter(|r| !r.warmup && r.ok).collect();
    let collect = |f: &dyn Fn(&RunRecord) -> u64| -> Vec<u64> { counted.iter().map(|r| f(r)).collect() };
    let wall_us = summarize(&collect(&|r| r.wall_us))?;
    let peak = summarize(&collect(&|r| r.memory.peak_tree_bytes))?;
    let mean = summarize(&collect(&|r| r.memory.mean_tree_bytes))?;
    let distinct = summarize(&collect(&|r| u64::from(r.memory.distinct_processes)))?;
    let names: BTreeSet<&str> = counted
        .iter()
        .flat_map(|r| r.memory.by_name.iter().map(|n| n.name.as_str()))
        .collect();
    let mut by_name: Vec<NameSummary> = Vec::new();
    for name in names {
        let mut peaks = Vec::new();
        let mut seen = 0u32;
        let mut max_concurrent = 0;
        for run in &counted {
            match run.memory.by_name.iter().find(|n| n.name == name) {
                Some(n) => {
                    peaks.push(n.peak_bytes);
                    seen += 1;
                    max_concurrent = max_concurrent.max(n.max_concurrent);
                }
                None => peaks.push(0),
            }
        }
        if let Some(peak_bytes) = summarize(&peaks) {
            by_name.push(NameSummary {
                name: name.to_owned(),
                peak_bytes,
                runs_seen: seen,
                max_concurrent,
            });
        }
    }
    by_name.sort_by(|a, b| {
        b.peak_bytes
            .median
            .cmp(&a.peak_bytes.median)
            .then(a.name.cmp(&b.name))
    });
    Some(RunSummary {
        runs: u32::try_from(counted.len()).unwrap_or(u32::MAX),
        wall_us,
        peak_tree_bytes: peak,
        mean_tree_bytes: mean,
        distinct_processes: distinct,
        by_name,
    })
}

/// Bytes to MiB.
pub fn mib(bytes: u64) -> f64 {
    // u64 -> f64 can round above 2^53 bytes (8 PiB); irrelevant for memory sizes.
    #[allow(clippy::cast_precision_loss)]
    let value = bytes as f64;
    value / 1_048_576.0
}

/// Microseconds to milliseconds.
pub fn ms(us: u64) -> f64 {
    #[allow(clippy::cast_precision_loss)]
    let value = us as f64;
    value / 1_000.0
}

/// Compares the summary medians with the target.
pub fn check_target(
    target: &crate::scenario::Target,
    summary: Option<&RunSummary>,
) -> TargetCheck {
    let wall_within = match (target.wall_ms, summary) {
        (Some(bound), Some(s)) => Some(ms(s.wall_us.median) <= bound),
        _ => None,
    };
    let peak_within = match (target.peak_tree_mib, summary) {
        (Some(bound), Some(s)) => Some(mib(s.peak_tree_bytes.median) <= bound),
        _ => None,
    };
    TargetCheck {
        source: target.source.clone(),
        wall_ms_max: target.wall_ms,
        wall_within,
        peak_tree_mib_max: target.peak_tree_mib,
        peak_within,
    }
}

fn status_word(status: Status) -> &'static str {
    match status {
        Status::Ok => "ok",
        Status::Failed => "FAILED",
        Status::Skipped => "skipped",
        Status::Pending => "pending",
    }
}

fn exit_text(run: &RunRecord) -> String {
    if run.timed_out {
        "timeout".to_owned()
    } else if run.stopped_after_duration {
        "stopped".to_owned()
    } else if let Some(code) = run.exit_code {
        code.to_string()
    } else if let Some(signal) = run.signal {
        format!("sig{signal}")
    } else {
        "-".to_owned()
    }
}

/// Renders the human-readable report.
pub fn render_table(result: &BenchResult) -> String {
    let mut out = String::new();
    let title = match (&result.scenario, &result.attach) {
        (Some(s), _) => format!("scenario {}", s.name),
        (None, Some(a)) => match &a.name {
            Some(name) => format!("attach --name {name}"),
            None => "attach --pid".to_owned(),
        },
        (None, None) => "result".to_owned(),
    };
    let _ = writeln!(
        out,
        "argus-bench {} | {title} | status {}",
        result.tool.version,
        status_word(result.status)
    );
    if let Some(reason) = &result.status_reason {
        let _ = writeln!(out, "  {reason}");
    }
    let env = &result.environment;
    let _ = writeln!(
        out,
        "  {} {} {} | {} | {} logical CPUs | {:.1} GiB RAM",
        env.os,
        env.os_name.as_deref().unwrap_or("?"),
        env.os_version.as_deref().unwrap_or("?"),
        env.arch,
        env.logical_cpus,
        mib(env.total_ram_bytes) / 1024.0
    );
    let _ = writeln!(
        out,
        "  metric {} sampled every {} ms, summed over the process tree",
        result.metric.name, result.metric.interval_ms
    );
    if result.runs.is_empty() {
        return out;
    }
    let _ = writeln!(out);
    let _ = writeln!(
        out,
        "  {:<6} {:>8} {:>11} {:>14} {:>14} {:>6} {:>8} {:>9}",
        "run", "exit", "wall ms", "peak tree MiB", "mean tree MiB", "procs", "samples", "leftover"
    );
    for run in &result.runs {
        let label = if run.warmup {
            format!("w{}", run.index)
        } else {
            run.index.to_string()
        };
        let _ = writeln!(
            out,
            "  {:<6} {:>8} {:>11.1} {:>14.1} {:>14.1} {:>6} {:>8} {:>9}{}",
            label,
            exit_text(run),
            ms(run.wall_us),
            mib(run.memory.peak_tree_bytes),
            mib(run.memory.mean_tree_bytes),
            run.memory.distinct_processes,
            run.memory.samples,
            run.leftover_processes,
            if run.ok { "" } else { "  <- failed" }
        );
    }
    if let Some(summary) = &result.summary {
        let _ = writeln!(out);
        let _ = writeln!(out, "  summary over {} counted run(s):", summary.runs);
        let _ = writeln!(
            out,
            "  {:<22} {:>10} {:>10} {:>10}",
            "", "median", "min", "max"
        );
        let rows: [(&str, Summary, bool); 3] = [
            ("wall ms", summary.wall_us, false),
            ("peak tree MiB", summary.peak_tree_bytes, true),
            ("mean tree MiB", summary.mean_tree_bytes, true),
        ];
        for (label, s, is_bytes) in rows {
            let f = |v: u64| if is_bytes { mib(v) } else { ms(v) };
            let _ = writeln!(
                out,
                "  {:<22} {:>10.1} {:>10.1} {:>10.1}",
                label,
                f(s.median),
                f(s.min),
                f(s.max)
            );
        }
        let d = summary.distinct_processes;
        let _ = writeln!(
            out,
            "  {:<22} {:>10} {:>10} {:>10}",
            "processes", d.median, d.min, d.max
        );
        if !summary.by_name.is_empty() {
            let _ = writeln!(out);
            let _ = writeln!(out, "  peak by process name (median of per-run peaks):");
            let total = summary.peak_tree_bytes.median.max(1);
            for name in &summary.by_name {
                #[allow(clippy::cast_precision_loss)]
                let share = name.peak_bytes.median as f64 * 100.0 / total as f64;
                let _ = writeln!(
                    out,
                    "    {:<28} {:>9.1} MiB  {:>5.1}%  (max {} at once, in {}/{} runs)",
                    name.name,
                    mib(name.peak_bytes.median),
                    share,
                    name.max_concurrent,
                    name.runs_seen,
                    summary.runs
                );
            }
        }
    }
    if let Some(target) = &result.target {
        let _ = writeln!(out);
        let verdict = |v: Option<bool>| match v {
            Some(true) => "within",
            Some(false) => "OVER",
            None => "n/a",
        };
        let source = target.source.as_deref().unwrap_or("scenario target");
        if let Some(bound) = target.wall_ms_max {
            let _ = writeln!(
                out,
                "  target ({source}): wall <= {bound} ms: {}",
                verdict(target.wall_within)
            );
        }
        if let Some(bound) = target.peak_tree_mib_max {
            let _ = writeln!(
                out,
                "  target ({source}): peak tree <= {bound} MiB: {}",
                verdict(target.peak_within)
            );
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tree::NamePeak;

    fn run(index: u32, warmup: bool, ok: bool, wall_us: u64, peak: u64) -> RunRecord {
        RunRecord {
            index,
            warmup,
            exit_code: Some(0),
            signal: None,
            stopped_after_duration: false,
            timed_out: false,
            ok,
            wall_us,
            memory: RunMemory {
                peak_tree_bytes: peak,
                peak_at_ms: 0,
                mean_tree_bytes: peak / 2,
                samples: 3,
                counted_samples: 3,
                distinct_processes: 2,
                by_name: vec![NamePeak {
                    name: "py".to_owned(),
                    peak_bytes: peak,
                    max_concurrent: 1,
                    distinct: 1,
                }],
            },
            leftover_processes: 0,
        }
    }

    #[test]
    fn summary_excludes_warmup_and_failed_runs() {
        let runs = vec![
            run(1, true, true, 9_000, 900),
            run(2, false, true, 1_000, 10),
            run(3, false, true, 3_000, 30),
            run(4, false, false, 99_000, 990),
            run(5, false, true, 2_000, 20),
        ];
        let s = summarize_runs(&runs).unwrap();
        assert_eq!(s.runs, 3);
        assert_eq!((s.wall_us.median, s.wall_us.min, s.wall_us.max), (2_000, 1_000, 3_000));
        assert_eq!(s.peak_tree_bytes.median, 20);
        assert_eq!(s.by_name[0].name, "py");
        assert_eq!(s.by_name[0].runs_seen, 3);
    }

    #[test]
    fn names_missing_from_a_run_count_as_zero() {
        let mut runs = vec![run(1, false, true, 1, 100), run(2, false, true, 1, 100)];
        runs[1].memory.by_name.push(NamePeak {
            name: "node".to_owned(),
            peak_bytes: 50,
            max_concurrent: 1,
            distinct: 1,
        });
        let s = summarize_runs(&runs).unwrap();
        let node = s.by_name.iter().find(|n| n.name == "node").unwrap();
        assert_eq!((node.peak_bytes.min, node.peak_bytes.max), (0, 50));
        assert_eq!(node.runs_seen, 1);
    }

    #[test]
    fn no_counted_runs_means_no_summary() {
        assert!(summarize_runs(&[run(1, true, true, 1, 1)]).is_none());
    }

    #[test]
    fn target_check_uses_medians() {
        let runs = vec![run(1, false, true, 20_000, 10 * 1_048_576)];
        let s = summarize_runs(&runs).unwrap();
        let t = crate::scenario::Target {
            wall_ms: Some(30.0),
            peak_tree_mib: Some(5.0),
            source: None,
        };
        let c = check_target(&t, Some(&s));
        assert_eq!(c.wall_within, Some(true));
        assert_eq!(c.peak_within, Some(false));
    }
}
