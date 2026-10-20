//! `argus-bench attach`: sample an already running process tree for a fixed time.
//!
//! Used for applications a person drives by hand (the desktop app and its webview children)
//! and for idle measurements. Roots are selected by PID or by process name; with a name, every
//! matching process whose ancestors do not also match becomes a root.

use std::collections::{BTreeMap, HashMap};
use std::time::{Duration, Instant};

use anyhow::bail;

use crate::process::{ProcId, ProcInfo, ProcessSource};
use crate::report::{AttachInfo, BenchResult, RunRecord, Status, summarize_runs};
use crate::sampler::{LoopLimits, NeverExits, sample_loop};
use crate::tree::TreeTracker;

/// Options of `argus-bench attach`.
#[derive(Debug, Clone)]
pub struct AttachOptions {
    /// Root PIDs.
    pub pids: Vec<u32>,
    /// Root process names (case-insensitive; a trailing `.exe` is optional).
    pub names: Vec<String>,
    /// Sampling window.
    pub duration: Duration,
    /// Sampling interval.
    pub interval: Duration,
    /// Samples before this offset do not count towards peaks.
    pub measure_after: Duration,
    /// Result timestamp (already normalized); `None` reads the clock.
    pub timestamp: Option<String>,
    /// Free-form labels recorded in the result.
    pub labels: BTreeMap<String, String>,
}

fn normalized(name: &str) -> String {
    let lower = name.to_ascii_lowercase();
    lower
        .strip_suffix(".exe")
        .map(str::to_owned)
        .unwrap_or(lower)
}

fn name_matches(process: &ProcInfo, wanted: &str) -> bool {
    let wanted = normalized(wanted);
    normalized(&process.name) == wanted || normalized(&process.os_name) == wanted
}

/// Finds the root processes for attach mode. `self_pid` (the harness) is never selected.
pub fn find_roots(
    snapshot: &[ProcInfo],
    pids: &[u32],
    names: &[String],
    self_pid: u32,
) -> anyhow::Result<Vec<ProcId>> {
    if pids.is_empty() && names.is_empty() {
        bail!("give --pid or --name");
    }
    let by_pid: HashMap<u32, &ProcInfo> = snapshot.iter().map(|p| (p.pid, p)).collect();
    let mut roots: Vec<ProcId> = Vec::new();
    for pid in pids {
        match by_pid.get(pid) {
            Some(p) => roots.push(p.id()),
            None => bail!("no running process with PID {pid}"),
        }
    }
    for name in names {
        let matches: Vec<&ProcInfo> = snapshot
            .iter()
            .filter(|p| p.pid != self_pid && name_matches(p, name))
            .collect();
        if matches.is_empty() {
            bail!("no running process named `{name}`");
        }
        let matched: std::collections::HashSet<u32> = matches.iter().map(|p| p.pid).collect();
        for process in matches {
            // Keep only the top-most match: skip a process whose ancestor also matches.
            let mut ancestor = process.parent;
            let mut nested = false;
            let mut steps = 0;
            while let Some(parent) = ancestor {
                if steps > 4_096 || parent == process.pid {
                    break;
                }
                steps += 1;
                if matched.contains(&parent) {
                    nested = true;
                    break;
                }
                ancestor = by_pid
                    .get(&parent)
                    .and_then(|p| p.parent)
                    .filter(|&next| next != parent);
            }
            if !nested {
                roots.push(process.id());
            }
        }
    }
    roots.sort();
    roots.dedup();
    Ok(roots)
}

/// Samples the tree below the selected roots and returns the result document.
pub fn attach(opts: &AttachOptions, source: &mut dyn ProcessSource) -> anyhow::Result<BenchResult> {
    let snapshot = source.snapshot();
    let roots = find_roots(&snapshot, &opts.pids, &opts.names, std::process::id())?;
    let mut tracker = TreeTracker::with_roots(&roots);
    let limits = LoopLimits {
        interval: opts.interval,
        duration: Some(opts.duration),
        timeout: opts.duration.saturating_add(Duration::from_secs(3_600)),
        measure_after: opts.measure_after,
        stop_when_tree_gone: true,
        terminate_on_stop: false,
    };
    let start = Instant::now();
    let outcome = sample_loop(source, &mut tracker, &mut NeverExits, &limits, start);
    let wall = outcome.end.saturating_duration_since(start);
    let wall_ms = u64::try_from(wall.as_millis()).unwrap_or(u64::MAX);
    let ok = outcome.stopped_after_duration;
    let note = (!ok).then(|| format!("the measured tree exited after {wall_ms} ms"));

    let mut result =
        crate::runner::attach_base(opts.timestamp.as_ref(), opts.interval, &opts.labels);
    result.attach = Some(AttachInfo {
        selector: match (opts.pids.is_empty(), opts.names.is_empty()) {
            (false, true) => "pid",
            (true, false) => "name",
            _ => "pid+name",
        }
        .to_owned(),
        names: opts.names.clone(),
        roots: u32::try_from(roots.len()).unwrap_or(u32::MAX),
        duration_ms: u64::try_from(opts.duration.as_millis()).unwrap_or(u64::MAX),
    });
    result.runs.push(RunRecord {
        index: 1,
        warmup: false,
        exit_code: None,
        signal: None,
        stopped_after_duration: outcome.stopped_after_duration,
        timed_out: false,
        ok,
        wall_us: u64::try_from(wall.as_micros()).unwrap_or(u64::MAX),
        memory: outcome.memory,
        leftover_processes: 0,
        leftover_names: Vec::new(),
        note: note.clone(),
    });
    result.summary = summarize_runs(&result.runs);
    if !ok {
        result.status = Status::Failed;
        result.status_reason = note;
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::sampler::fake::{FakeSource, proc};

    fn table() -> Vec<ProcInfo> {
        vec![
            proc(1, None, "init", 1),
            proc(10, Some(1), "argus-gui.exe", 5),
            proc(11, Some(10), "pythonw.exe", 50),
            proc(12, Some(11), "msedgewebview2.exe", 100),
            proc(13, Some(12), "msedgewebview2.exe", 80),
            proc(20, Some(1), "msedgewebview2.exe", 70),
            proc(99, Some(1), "argus-bench", 3),
        ]
    }

    #[test]
    fn name_selects_topmost_matches_case_insensitively() {
        let roots = find_roots(&table(), &[], &["MSEdgeWebView2".to_owned()], 99).unwrap();
        let pids: Vec<u32> = roots.iter().map(|r| r.pid).collect();
        assert_eq!(pids, vec![12, 20]);
    }

    #[test]
    fn pid_and_errors() {
        let roots = find_roots(&table(), &[10], &[], 99).unwrap();
        assert_eq!(roots[0].pid, 10);
        assert!(find_roots(&table(), &[4242], &[], 99).is_err());
        assert!(find_roots(&table(), &[], &["nope".to_owned()], 99).is_err());
        assert!(find_roots(&table(), &[], &[], 99).is_err());
        // The harness never selects itself.
        assert!(find_roots(&table(), &[], &["argus-bench".to_owned()], 99).is_err());
    }

    #[test]
    fn attach_measures_the_whole_tree_for_the_duration() {
        let mut source = FakeSource::new(vec![table()]);
        let opts = AttachOptions {
            pids: vec![],
            names: vec!["argus-gui".to_owned()],
            duration: Duration::from_millis(30),
            interval: Duration::from_millis(5),
            measure_after: Duration::ZERO,
            timestamp: Some("2026-10-08T00:00:00Z".to_owned()),
            labels: BTreeMap::new(),
        };
        let result = attach(&opts, &mut source).unwrap();
        assert_eq!(result.status, Status::Ok);
        let summary = result.summary.unwrap();
        assert_eq!(summary.peak_tree_bytes.median, 235);
        assert_eq!(summary.distinct_processes.median, 4);
        assert_eq!(summary.by_name[0].name, "msedgewebview2.exe");
        assert_eq!(summary.by_name[0].peak_bytes.median, 180);
        assert_eq!(result.attach.unwrap().roots, 1);
        assert_eq!(result.created_utc, "2026-10-08T00:00:00Z");
    }
}
