//! The sampling loop shared by `run` and `attach`.
//!
//! Every `interval` the loop takes a snapshot of the process table, finds the measured tree
//! ([`TreeTracker`]) and adds the sample to a [`RunAccumulator`]. Between samples it waits for
//! the root process to exit ([`RootWaiter`]), so the wall time is taken when the process exits,
//! not rounded up to the next sample.

use std::time::{Duration, Instant};

use crate::process::{ProcId, ProcInfo, ProcessSource};
use crate::tree::{RunAccumulator, RunMemory, TreeTracker};

/// How the root process ended.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RootExit {
    /// When the exit was observed.
    pub at: Instant,
    /// Exit code, when the process exited with one.
    pub code: Option<i32>,
    /// Signal that ended the process (Unix only).
    pub signal: Option<i32>,
}

/// Waits for the root process to exit.
pub trait RootWaiter {
    /// Blocks for at most `timeout`; returns the exit once the root has exited.
    fn wait_for(&mut self, timeout: Duration) -> Option<RootExit>;
}

/// A waiter for a tree whose root the harness did not spawn (attach mode): it never reports an
/// exit, it only paces the loop. The loop ends when the tree is gone or the duration elapses.
#[derive(Debug, Default, Clone, Copy)]
pub struct NeverExits;

impl RootWaiter for NeverExits {
    fn wait_for(&mut self, timeout: Duration) -> Option<RootExit> {
        std::thread::sleep(timeout);
        None
    }
}

/// Limits of one sampling loop.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct LoopLimits {
    /// Time between samples.
    pub interval: Duration,
    /// Stop (and, in run mode, terminate the tree) once this much time has passed. Expected
    /// for applications that do not exit by themselves.
    pub duration: Option<Duration>,
    /// Hard limit: terminate the tree once this much time has passed.
    pub timeout: Duration,
    /// Samples before this offset do not count towards peaks.
    pub measure_after: Duration,
    /// End the loop when no process of the tree is left (attach mode).
    pub stop_when_tree_gone: bool,
    /// Terminate the remaining tree when `duration` or `timeout` is reached (run mode).
    pub terminate_on_stop: bool,
}

/// What a sampling loop observed.
#[derive(Debug, Clone, PartialEq)]
pub struct LoopOutcome {
    /// Memory measurement.
    pub memory: RunMemory,
    /// Root exit, when it was observed.
    pub exit: Option<RootExit>,
    /// The loop stopped because `duration` elapsed.
    pub stopped_after_duration: bool,
    /// The loop stopped because `timeout` elapsed.
    pub timed_out: bool,
    /// The loop stopped because no process of the tree was left.
    pub tree_gone: bool,
    /// When the loop ended (the root exit time when the root exited).
    pub end: Instant,
    /// Members of the tree still alive after the root exited or the loop stopped.
    pub leftover: Vec<ProcInfo>,
}

/// How long to wait for the root to exit after the harness terminated the tree.
const EXIT_GRACE: Duration = Duration::from_secs(10);

fn millis(d: Duration) -> u64 {
    u64::try_from(d.as_millis()).unwrap_or(u64::MAX)
}

/// Runs the sampling loop until the root exits, the duration or timeout elapses, or (with
/// `stop_when_tree_gone`) the tree is empty. The first sample is taken immediately.
pub fn sample_loop(
    source: &mut dyn ProcessSource,
    tracker: &mut TreeTracker,
    waiter: &mut dyn RootWaiter,
    limits: &LoopLimits,
    start: Instant,
) -> LoopOutcome {
    let interval = limits.interval.max(Duration::from_millis(1));
    let mut acc = RunAccumulator::new(millis(limits.measure_after));
    let mut next = start;
    let mut seen_any = false;
    let mut stopped_after_duration = false;
    let mut timed_out = false;
    let mut tree_gone = false;
    let mut last_alive: Vec<ProcId>;
    let mut exit: Option<RootExit> = None;

    loop {
        let elapsed = start.elapsed();
        let sample = tracker.observe(&source.snapshot());
        acc.add(millis(elapsed), &sample);
        last_alive = sample
            .processes
            .iter()
            .map(crate::process::ProcInfo::id)
            .collect();
        seen_any |= !sample.processes.is_empty();

        if limits.stop_when_tree_gone && seen_any && sample.processes.is_empty() {
            tree_gone = true;
            break;
        }
        if elapsed >= limits.timeout {
            timed_out = true;
            break;
        }
        if limits.duration.is_some_and(|d| elapsed >= d) {
            stopped_after_duration = true;
            break;
        }

        let now = Instant::now();
        while next <= now {
            next += interval;
        }
        // Wait for the root to exit until the next sample is due (at most one interval).
        if let Some(found) = waiter.wait_for(next.saturating_duration_since(Instant::now())) {
            exit = Some(found);
            break;
        }
    }

    let mut end = exit.map_or_else(Instant::now, |e| e.at);
    if (stopped_after_duration || timed_out) && limits.terminate_on_stop {
        source.terminate(&last_alive);
        if let Some(found) = waiter.wait_for(EXIT_GRACE) {
            exit = Some(found);
        }
        end = Instant::now();
    }

    // Members still alive once the root is gone (daemonised children, slow shutdown).
    let leftover: Vec<ProcInfo> = if limits.stop_when_tree_gone {
        Vec::new()
    } else {
        tracker.observe(&source.snapshot()).processes
    };

    LoopOutcome {
        memory: acc.finish(),
        exit,
        stopped_after_duration,
        timed_out,
        tree_gone,
        end,
        leftover,
    }
}

#[cfg(test)]
pub(crate) mod fake {
    //! Scripted process source and waiter for unit tests.

    use std::collections::VecDeque;
    use std::time::{Duration, Instant};

    use super::{RootExit, RootWaiter};
    use crate::process::{ProcId, ProcInfo, ProcessSource};

    /// Returns the scripted snapshots in order, then repeats the last one.
    #[derive(Debug, Default)]
    pub(crate) struct FakeSource {
        pub(crate) script: VecDeque<Vec<ProcInfo>>,
        pub(crate) last: Vec<ProcInfo>,
        pub(crate) terminated: Vec<ProcId>,
        pub(crate) snapshots: usize,
    }

    impl FakeSource {
        pub(crate) fn new(script: Vec<Vec<ProcInfo>>) -> Self {
            Self {
                script: script.into(),
                ..Self::default()
            }
        }
    }

    impl ProcessSource for FakeSource {
        fn snapshot(&mut self) -> Vec<ProcInfo> {
            self.snapshots += 1;
            if let Some(next) = self.script.pop_front() {
                self.last = next;
            }
            self.last.clone()
        }

        fn terminate(&mut self, ids: &[ProcId]) -> usize {
            self.terminated.extend_from_slice(ids);
            // After termination the table no longer shows the terminated processes.
            self.last.retain(|p| !ids.contains(&p.id()));
            self.script.clear();
            ids.len()
        }
    }

    /// Reports an exit after `polls` waits; never sleeps.
    #[derive(Debug)]
    pub(crate) struct FakeWaiter {
        pub(crate) polls: Option<usize>,
        pub(crate) code: i32,
    }

    impl RootWaiter for FakeWaiter {
        fn wait_for(&mut self, _timeout: Duration) -> Option<RootExit> {
            match self.polls.as_mut() {
                Some(0) => Some(RootExit {
                    at: Instant::now(),
                    code: Some(self.code),
                    signal: None,
                }),
                Some(n) => {
                    *n -= 1;
                    None
                }
                None => None,
            }
        }
    }

    pub(crate) fn proc(pid: u32, parent: Option<u32>, name: &str, mem: u64) -> ProcInfo {
        ProcInfo {
            pid,
            parent,
            start_time: 100,
            name: name.to_owned(),
            os_name: name.to_owned(),
            memory_bytes: mem,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::fake::{FakeSource, FakeWaiter, proc};
    use super::*;

    fn limits() -> LoopLimits {
        LoopLimits {
            interval: Duration::from_millis(1),
            duration: None,
            timeout: Duration::from_secs(30),
            measure_after: Duration::ZERO,
            stop_when_tree_gone: false,
            terminate_on_stop: true,
        }
    }

    #[test]
    fn samples_until_root_exits_and_reports_leftovers() {
        let mut source = FakeSource::new(vec![
            vec![
                proc(1, None, "init", 1_000),
                proc(10, Some(1), "python", 10),
            ],
            vec![
                proc(10, Some(1), "python", 20),
                proc(11, Some(10), "node", 30),
                proc(12, Some(11), "chrome", 300),
            ],
            vec![
                proc(10, Some(1), "python", 25),
                proc(11, Some(10), "node", 30),
            ],
            // Root gone, node re-parented to init and still alive.
            vec![proc(1, None, "init", 1_000), proc(11, Some(1), "node", 30)],
        ]);
        let mut waiter = FakeWaiter {
            polls: Some(2),
            code: 0,
        };
        let mut tracker = TreeTracker::new(&[10]);
        let out = sample_loop(
            &mut source,
            &mut tracker,
            &mut waiter,
            &limits(),
            Instant::now(),
        );
        assert_eq!(out.exit.map(|e| e.code), Some(Some(0)));
        assert_eq!(out.memory.samples, 3);
        assert_eq!(out.memory.peak_tree_bytes, 350);
        assert_eq!(out.memory.distinct_processes, 3);
        assert_eq!(out.leftover.len(), 1);
        assert_eq!(out.leftover[0].pid, 11);
        assert!(!out.timed_out && !out.stopped_after_duration);
    }

    #[test]
    fn timeout_terminates_the_tree() {
        let mut source = FakeSource::new(vec![vec![
            proc(10, None, "sh", 10),
            proc(11, Some(10), "sleep", 5),
        ]]);
        let mut waiter = FakeWaiter {
            polls: None,
            code: 0,
        };
        let mut tracker = TreeTracker::new(&[10]);
        let lim = LoopLimits {
            timeout: Duration::from_millis(20),
            ..limits()
        };
        let out = sample_loop(&mut source, &mut tracker, &mut waiter, &lim, Instant::now());
        assert!(out.timed_out);
        assert_eq!(source.terminated.len(), 2);
        assert!(out.leftover.is_empty());
        assert_eq!(out.memory.peak_tree_bytes, 15);
    }

    #[test]
    fn duration_stop_without_termination_in_attach_mode() {
        let mut source = FakeSource::new(vec![vec![proc(10, None, "app", 10)]]);
        let mut tracker = TreeTracker::new(&[10]);
        let lim = LoopLimits {
            duration: Some(Duration::from_millis(10)),
            stop_when_tree_gone: true,
            terminate_on_stop: false,
            ..limits()
        };
        let out = sample_loop(
            &mut source,
            &mut tracker,
            &mut NeverExits,
            &lim,
            Instant::now(),
        );
        assert!(out.stopped_after_duration);
        assert!(source.terminated.is_empty());
        assert!(out.memory.samples >= 2);
    }

    #[test]
    fn attach_ends_when_tree_is_gone() {
        let mut source = FakeSource::new(vec![
            vec![proc(10, None, "app", 10)],
            vec![proc(10, None, "app", 12)],
            vec![proc(1, None, "init", 1)],
        ]);
        let mut tracker = TreeTracker::new(&[10]);
        let lim = LoopLimits {
            duration: Some(Duration::from_secs(30)),
            stop_when_tree_gone: true,
            terminate_on_stop: false,
            ..limits()
        };
        let out = sample_loop(
            &mut source,
            &mut tracker,
            &mut NeverExits,
            &lim,
            Instant::now(),
        );
        assert!(out.tree_gone);
        assert_eq!(out.memory.samples, 3);
        assert_eq!(out.memory.peak_tree_bytes, 12);
    }
}
