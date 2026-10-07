//! Process-tree walk and per-run aggregation.
//!
//! Each sample finds the measured root process(es) and **all** descendants, then sums their
//! memory (spec §11: "whole-process-tree RSS"). A process seen once in the tree stays a member
//! while it lives ("sticky membership"), so a grandchild whose parent exits early and that is
//! re-parented to init (or a subreaper) is still counted. A process that starts and exits
//! between two samples is never seen; that limit is documented in bench/README.md.

use std::collections::{BTreeMap, BTreeSet, HashMap};

use serde::{Deserialize, Serialize};

use crate::process::{ProcId, ProcInfo};

/// Tracks which processes belong to the measured tree across samples.
#[derive(Debug, Clone)]
pub struct TreeTracker {
    roots: Vec<Root>,
    members: BTreeSet<ProcId>,
}

#[derive(Debug, Clone, Copy)]
struct Root {
    pid: u32,
    /// Filled from the first snapshot that shows the PID (or given up front).
    identity: Option<ProcId>,
}

/// The measured tree in one snapshot.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TreeSample {
    /// Sum of `memory_bytes` over every process in the tree.
    pub total_bytes: u64,
    /// The processes in the tree, roots first, then breadth-first.
    pub processes: Vec<ProcInfo>,
    /// Whether at least one root process is still alive.
    pub roots_alive: bool,
}

impl TreeTracker {
    /// Tracks the trees below the given root PIDs. A root's start time is taken from the first
    /// snapshot that contains its PID.
    pub fn new(root_pids: &[u32]) -> Self {
        Self {
            roots: root_pids
                .iter()
                .map(|&pid| Root {
                    pid,
                    identity: None,
                })
                .collect(),
            members: BTreeSet::new(),
        }
    }

    /// Tracks the trees below roots whose identity is already known (attach mode).
    pub fn with_roots(roots: &[ProcId]) -> Self {
        Self {
            roots: roots
                .iter()
                .map(|id| Root {
                    pid: id.pid,
                    identity: Some(*id),
                })
                .collect(),
            members: BTreeSet::new(),
        }
    }

    /// Every process identity that has been part of the tree so far.
    pub fn members(&self) -> &BTreeSet<ProcId> {
        &self.members
    }

    /// Finds the tree in `snapshot` and records its members.
    pub fn observe(&mut self, snapshot: &[ProcInfo]) -> TreeSample {
        let by_pid: HashMap<u32, &ProcInfo> = snapshot.iter().map(|p| (p.pid, p)).collect();
        let mut children: HashMap<u32, Vec<&ProcInfo>> = HashMap::new();
        for process in snapshot {
            if let Some(parent) = process.parent {
                if parent != process.pid {
                    children.entry(parent).or_default().push(process);
                }
            }
        }
        for list in children.values_mut() {
            list.sort_by_key(|p| p.pid);
        }

        let mut queue: Vec<&ProcInfo> = Vec::new();
        let mut roots_alive = false;
        for root in &mut self.roots {
            let Some(process) = by_pid.get(&root.pid) else {
                continue;
            };
            let identity = *root.identity.get_or_insert_with(|| process.id());
            if process.id() == identity {
                roots_alive = true;
                queue.push(process);
            }
        }
        // Sticky members: still alive (same PID and start time) even if re-parented.
        for member in &self.members {
            if let Some(process) = by_pid.get(&member.pid) {
                if process.start_time == member.start_time {
                    queue.push(process);
                }
            }
        }

        let mut visited: BTreeSet<u32> = BTreeSet::new();
        let mut ordered: Vec<&ProcInfo> = Vec::new();
        let mut index = 0;
        while index < queue.len() {
            let process = queue[index];
            index += 1;
            if !visited.insert(process.pid) {
                continue;
            }
            ordered.push(process);
            if let Some(kids) = children.get(&process.pid) {
                // A child cannot start before its parent; an older "child" means the parent
                // PID was reused and the link is not real.
                queue.extend(
                    kids.iter()
                        .filter(|kid| kid.start_time >= process.start_time)
                        .copied(),
                );
            }
        }

        let mut total: u64 = 0;
        for process in &ordered {
            total = total.saturating_add(process.memory_bytes);
            self.members.insert(process.id());
        }
        TreeSample {
            total_bytes: total,
            processes: ordered.into_iter().cloned().collect(),
            roots_alive,
        }
    }
}

/// Peak memory of all processes that share one name, within one run.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct NamePeak {
    /// Process name (executable file name when readable).
    pub name: String,
    /// Highest per-sample sum of memory over the processes with this name.
    pub peak_bytes: u64,
    /// Most processes with this name alive in one sample.
    pub max_concurrent: u32,
    /// Distinct processes with this name seen during the run.
    pub distinct: u32,
}

/// Memory measurement of one run (or one attach window).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct RunMemory {
    /// Highest summed tree memory over the counted samples.
    pub peak_tree_bytes: u64,
    /// Milliseconds from the start of sampling to the sample with the peak.
    pub peak_at_ms: u64,
    /// Mean summed tree memory over the counted samples that contained at least one process.
    pub mean_tree_bytes: u64,
    /// Samples taken in total.
    pub samples: u64,
    /// Samples that counted towards the peak (those taken at or after `measure_after`).
    pub counted_samples: u64,
    /// Distinct processes (PID + start time) seen in the tree.
    pub distinct_processes: u32,
    /// Per-name peaks, largest first.
    pub by_name: Vec<NamePeak>,
}

#[derive(Debug, Default, Clone)]
struct NameAcc {
    peak_bytes: u64,
    max_concurrent: u32,
    distinct: BTreeSet<ProcId>,
}

/// Accumulates samples of one run into a [`RunMemory`].
#[derive(Debug, Clone, Default)]
pub struct RunAccumulator {
    measure_after_ms: u64,
    peak_bytes: u64,
    peak_at_ms: u64,
    sum_bytes: u128,
    nonempty_counted: u64,
    samples: u64,
    counted: u64,
    distinct: BTreeSet<ProcId>,
    by_name: BTreeMap<String, NameAcc>,
}

fn to_u32(value: usize) -> u32 {
    u32::try_from(value).unwrap_or(u32::MAX)
}

impl RunAccumulator {
    /// Creates an accumulator. Samples taken before `measure_after_ms` still count towards the
    /// process inventory but not towards peaks (used to exclude start-up in idle scenarios).
    pub fn new(measure_after_ms: u64) -> Self {
        Self {
            measure_after_ms,
            ..Self::default()
        }
    }

    /// Adds one sample taken `elapsed_ms` after sampling started.
    pub fn add(&mut self, elapsed_ms: u64, sample: &TreeSample) {
        self.samples += 1;
        for process in &sample.processes {
            self.distinct.insert(process.id());
            self.by_name
                .entry(process.name.clone())
                .or_default()
                .distinct
                .insert(process.id());
        }
        if elapsed_ms < self.measure_after_ms {
            return;
        }
        self.counted += 1;
        if !sample.processes.is_empty() {
            self.nonempty_counted += 1;
            self.sum_bytes += u128::from(sample.total_bytes);
        }
        if sample.total_bytes > self.peak_bytes {
            self.peak_bytes = sample.total_bytes;
            self.peak_at_ms = elapsed_ms;
        }
        let mut per_name: BTreeMap<&str, (u64, u32)> = BTreeMap::new();
        for process in &sample.processes {
            let slot = per_name.entry(process.name.as_str()).or_default();
            slot.0 = slot.0.saturating_add(process.memory_bytes);
            slot.1 += 1;
        }
        for (name, (bytes, count)) in per_name {
            let acc = self.by_name.entry(name.to_owned()).or_default();
            acc.peak_bytes = acc.peak_bytes.max(bytes);
            acc.max_concurrent = acc.max_concurrent.max(count);
        }
    }

    /// Finishes the run.
    pub fn finish(self) -> RunMemory {
        let mean = if self.nonempty_counted == 0 {
            0
        } else {
            u64::try_from(self.sum_bytes / u128::from(self.nonempty_counted)).unwrap_or(u64::MAX)
        };
        let mut by_name: Vec<NamePeak> = self
            .by_name
            .into_iter()
            .map(|(name, acc)| NamePeak {
                name,
                peak_bytes: acc.peak_bytes,
                max_concurrent: acc.max_concurrent,
                distinct: to_u32(acc.distinct.len()),
            })
            .collect();
        by_name.sort_by(|a, b| b.peak_bytes.cmp(&a.peak_bytes).then(a.name.cmp(&b.name)));
        RunMemory {
            peak_tree_bytes: self.peak_bytes,
            peak_at_ms: self.peak_at_ms,
            mean_tree_bytes: mean,
            samples: self.samples,
            counted_samples: self.counted,
            distinct_processes: to_u32(self.distinct.len()),
            by_name,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn p(pid: u32, parent: Option<u32>, start: u64, name: &str, mem: u64) -> ProcInfo {
        ProcInfo {
            pid,
            parent,
            start_time: start,
            name: name.to_owned(),
            memory_bytes: mem,
        }
    }

    #[test]
    fn walks_all_descendants_and_ignores_unrelated() {
        let snap = vec![
            p(1, None, 0, "init", 1_000),
            p(10, Some(1), 5, "python", 100),
            p(11, Some(10), 6, "node", 50),
            p(12, Some(11), 7, "chromium", 300),
            p(13, Some(11), 7, "chromium", 200),
            p(20, Some(1), 5, "other", 9_999),
        ];
        let mut tracker = TreeTracker::new(&[10]);
        let sample = tracker.observe(&snap);
        assert_eq!(sample.total_bytes, 650);
        assert!(sample.roots_alive);
        let pids: Vec<u32> = sample.processes.iter().map(|p| p.pid).collect();
        assert_eq!(pids, vec![10, 11, 12, 13]);
    }

    #[test]
    fn rejects_pid_reuse_for_children_older_than_parent() {
        // PID 30 claims parent 10, but started before 10: its real parent died and 10 is reused.
        let snap = vec![p(10, None, 50, "root", 10), p(30, Some(10), 40, "stale", 500)];
        let mut tracker = TreeTracker::new(&[10]);
        assert_eq!(tracker.observe(&snap).total_bytes, 10);
    }

    #[test]
    fn root_identity_is_fixed_by_first_sighting() {
        let mut tracker = TreeTracker::new(&[10]);
        assert_eq!(tracker.observe(&[p(10, None, 5, "a", 7)]).total_bytes, 7);
        // Same PID, different start time: a different process, not our root.
        let sample = tracker.observe(&[p(10, None, 9, "b", 70)]);
        assert_eq!(sample.total_bytes, 0);
        assert!(!sample.roots_alive);
    }

    #[test]
    fn sticky_members_survive_reparenting() {
        let mut tracker = TreeTracker::new(&[10]);
        let first = vec![
            p(10, None, 1, "sh", 10),
            p(11, Some(10), 2, "daemon", 20),
            p(12, Some(11), 3, "worker", 30),
        ];
        assert_eq!(tracker.observe(&first).total_bytes, 60);
        // The root and the daemon exited; the worker was re-parented to PID 1.
        let second = vec![p(1, None, 0, "init", 1_000), p(12, Some(1), 3, "worker", 35)];
        let sample = tracker.observe(&second);
        assert_eq!(sample.total_bytes, 35);
        assert!(!sample.roots_alive);
        assert_eq!(tracker.members().len(), 3);
    }

    #[test]
    fn self_parent_and_cycles_terminate() {
        let snap = vec![p(0, Some(0), 0, "idle", 1), p(5, Some(6), 1, "a", 2)];
        let mut tracker = TreeTracker::new(&[0]);
        assert_eq!(tracker.observe(&snap).total_bytes, 1);
        let cyc = vec![p(5, Some(6), 1, "a", 2), p(6, Some(5), 1, "b", 3)];
        let mut tracker = TreeTracker::new(&[5]);
        assert_eq!(tracker.observe(&cyc).total_bytes, 5);
    }

    #[test]
    fn accumulator_tracks_peak_and_per_name_peaks() {
        let mut acc = RunAccumulator::new(0);
        let s1 = TreeSample {
            total_bytes: 300,
            processes: vec![p(1, None, 1, "py", 100), p(2, Some(1), 1, "chrome", 200)],
            roots_alive: true,
        };
        let s2 = TreeSample {
            total_bytes: 450,
            processes: vec![
                p(1, None, 1, "py", 50),
                p(2, Some(1), 1, "chrome", 150),
                p(3, Some(1), 1, "chrome", 250),
            ],
            roots_alive: true,
        };
        acc.add(0, &s1);
        acc.add(50, &s2);
        acc.add(100, &TreeSample::default());
        let run = acc.finish();
        assert_eq!(run.peak_tree_bytes, 450);
        assert_eq!(run.peak_at_ms, 50);
        assert_eq!(run.samples, 3);
        assert_eq!(run.counted_samples, 3);
        assert_eq!(run.mean_tree_bytes, 375);
        assert_eq!(run.distinct_processes, 3);
        assert_eq!(run.by_name[0].name, "chrome");
        assert_eq!(run.by_name[0].peak_bytes, 400);
        assert_eq!(run.by_name[0].max_concurrent, 2);
        assert_eq!(run.by_name[0].distinct, 2);
        assert_eq!(run.by_name[1].name, "py");
        assert_eq!(run.by_name[1].peak_bytes, 100);
    }

    #[test]
    fn measure_after_excludes_startup_from_peak() {
        let mut acc = RunAccumulator::new(1_000);
        let big = TreeSample {
            total_bytes: 900,
            processes: vec![p(1, None, 1, "app", 900)],
            roots_alive: true,
        };
        let small = TreeSample {
            total_bytes: 100,
            processes: vec![p(1, None, 1, "app", 100)],
            roots_alive: true,
        };
        acc.add(500, &big);
        acc.add(1_500, &small);
        let run = acc.finish();
        assert_eq!(run.peak_tree_bytes, 100);
        assert_eq!(run.peak_at_ms, 1_500);
        assert_eq!(run.samples, 2);
        assert_eq!(run.counted_samples, 1);
        assert_eq!(run.distinct_processes, 1);
    }
}
