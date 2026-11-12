//! Running one step: spawn with an explicit environment, feed stdin, capture output, enforce a
//! hard timeout and, on Unix, kill the whole process group (spec section 13 isolation).
//!
//! The child is started as the leader of a new process group, so the timeout also reaches
//! grandchildren (for example a web server or browser the command started). After a normal
//! exit the group is killed as well, so no stray process outlives its step.

use std::io::{Read, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, Instant};

/// Maximum bytes captured per stream; the rest is drained and dropped.
pub const MAX_CAPTURE: usize = 4 * 1024 * 1024;

/// What to run.
#[derive(Debug, Clone)]
pub struct ProcSpec {
    pub argv: Vec<String>,
    pub cwd: PathBuf,
    /// The complete environment; nothing is inherited.
    pub env: Vec<(String, String)>,
    pub stdin: Option<Vec<u8>>,
    pub timeout: Duration,
}

/// What happened.
#[derive(Debug, Clone, Default)]
pub struct ProcResult {
    /// Exit code; `None` when the process was killed by a signal or never reaped.
    pub exit_code: Option<i32>,
    /// True when the deadline passed before the process exited and its output closed.
    pub timed_out: bool,
    pub stdout: Vec<u8>,
    pub stderr: Vec<u8>,
    pub duration: Duration,
}

/// Spawn failures (missing executable, bad working directory, ...).
#[derive(Debug)]
pub struct SpawnError(pub String);

impl std::fmt::Display for SpawnError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for SpawnError {}

fn reader<R: Read + Send + 'static>(
    mut src: R,
    tag: u8,
    tx: mpsc::Sender<(u8, Vec<u8>)>,
) -> thread::JoinHandle<()> {
    thread::spawn(move || {
        let mut kept = Vec::new();
        let mut buf = [0u8; 8192];
        loop {
            match src.read(&mut buf) {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    let room = MAX_CAPTURE.saturating_sub(kept.len());
                    kept.extend_from_slice(&buf[..n.min(room)]);
                }
            }
        }
        let _ = tx.send((tag, kept));
    })
}

/// Kill the process group led by `child` (Unix) and the child itself.
pub fn kill_tree(child: &mut Child) {
    #[cfg(unix)]
    {
        use rustix::process::{Pid, Signal, kill_process_group};
        if let Some(pid) = Pid::from_raw(i32::try_from(child.id()).unwrap_or(0)) {
            // ESRCH (group already gone) is expected after a clean exit.
            let _ = kill_process_group(pid, Signal::KILL);
        }
    }
    let _ = child.kill();
}

/// Run a process to completion or timeout.
pub fn run(spec: &ProcSpec) -> Result<ProcResult, SpawnError> {
    let (exe, args) = spec
        .argv
        .split_first()
        .ok_or_else(|| SpawnError("empty command line".into()))?;
    let mut cmd = Command::new(exe);
    cmd.args(args)
        .current_dir(&spec.cwd)
        .env_clear()
        .envs(spec.env.iter().map(|(k, v)| (k, v)))
        .stdin(if spec.stdin.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }
    let start = Instant::now();
    let deadline = start + spec.timeout;
    let mut child = cmd
        .spawn()
        .map_err(|e| SpawnError(format!("cannot start {exe:?}: {e}")))?;

    let (tx, rx) = mpsc::channel();
    let mut handles = Vec::new();
    if let Some(out) = child.stdout.take() {
        handles.push(reader(out, 1, tx.clone()));
    }
    if let Some(err) = child.stderr.take() {
        handles.push(reader(err, 2, tx.clone()));
    }
    drop(tx);
    if let (Some(mut sink), Some(data)) = (child.stdin.take(), spec.stdin.clone()) {
        // Broken pipe (the child did not read its input) is not an error of the runner.
        handles.push(thread::spawn(move || {
            let _ = sink.write_all(&data);
        }));
    }

    let mut result = ProcResult::default();
    let mut status = None;
    while Instant::now() < deadline {
        match child.try_wait() {
            Ok(Some(s)) => {
                status = Some(s);
                break;
            }
            Ok(None) => thread::sleep(Duration::from_millis(10)),
            Err(_) => break,
        }
    }
    let mut streams_open = 2usize;
    if status.is_some() {
        // The process exited; wait for its output to close, still bounded by the deadline
        // (a grandchild may hold the pipes open).
        while streams_open > 0 {
            let left = deadline.saturating_duration_since(Instant::now());
            match rx.recv_timeout(left) {
                Ok((tag, bytes)) => {
                    store(&mut result, tag, bytes);
                    streams_open -= 1;
                }
                Err(_) => break,
            }
        }
    }
    if status.is_none() || streams_open > 0 {
        result.timed_out = true;
    }
    kill_tree(&mut child);
    if status.is_none() {
        status = child.wait().ok();
    }
    // After the kill every pipe writer is gone, so the readers finish promptly; the grace
    // period only guards against a process that escaped its group.
    let grace = Instant::now() + Duration::from_secs(5);
    while streams_open > 0 {
        match rx.recv_timeout(grace.saturating_duration_since(Instant::now())) {
            Ok((tag, bytes)) => {
                store(&mut result, tag, bytes);
                streams_open -= 1;
            }
            Err(_) => break,
        }
    }
    if streams_open == 0 {
        for h in handles {
            let _ = h.join();
        }
    }
    result.exit_code = status.and_then(|s| s.code());
    result.duration = start.elapsed();
    Ok(result)
}

fn store(result: &mut ProcResult, tag: u8, bytes: Vec<u8>) {
    if tag == 1 {
        result.stdout = bytes;
    } else {
        result.stderr = bytes;
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    fn sh(script: &str, timeout_ms: u64) -> ProcResult {
        let dir = tempfile::tempdir().expect("temp dir");
        run(&ProcSpec {
            argv: vec!["/bin/sh".into(), "-c".into(), script.into()],
            cwd: dir.path().to_path_buf(),
            env: vec![
                ("PATH".into(), "/usr/bin:/bin".into()),
                ("MARK".into(), "m1".into()),
            ],
            stdin: None,
            timeout: Duration::from_millis(timeout_ms),
        })
        .expect("spawn /bin/sh")
    }

    #[test]
    fn captures_streams_and_exit_code() {
        let r = sh("echo out; echo err >&2; exit 3", 5000);
        assert_eq!(r.exit_code, Some(3));
        assert!(!r.timed_out);
        assert_eq!(r.stdout, b"out\n");
        assert_eq!(r.stderr, b"err\n");
    }

    #[test]
    fn environment_is_exactly_what_was_given() {
        let r = sh("env | sort", 5000);
        let text = String::from_utf8(r.stdout).unwrap();
        assert!(text.contains("MARK=m1"));
        assert!(!text.contains("HOME="), "{text}");
    }

    #[test]
    fn stdin_is_fed_then_closed() {
        let dir = tempfile::tempdir().unwrap();
        let r = run(&ProcSpec {
            argv: vec!["/bin/sh".into(), "-c".into(), "cat; echo done".into()],
            cwd: dir.path().to_path_buf(),
            env: vec![("PATH".into(), "/usr/bin:/bin".into())],
            stdin: Some(b"hello\n".to_vec()),
            timeout: Duration::from_secs(5),
        })
        .unwrap();
        assert_eq!(r.stdout, b"hello\ndone\n");
        assert_eq!(r.exit_code, Some(0));
    }

    #[test]
    fn timeout_kills_the_whole_process_group() {
        let dir = tempfile::tempdir().unwrap();
        let marker = dir.path().join("grandchild-survived");
        // A background grandchild that would write the marker after 2 s, and a leader that
        // never exits on its own.
        let script = format!(
            "(sleep 2; echo alive > '{}') & echo started; sleep 30",
            marker.display()
        );
        let start = Instant::now();
        let r = sh(&script, 500);
        assert!(r.timed_out);
        assert_eq!(r.exit_code, None, "killed by signal");
        assert_eq!(r.stdout, b"started\n", "output before the timeout is kept");
        assert!(start.elapsed() < Duration::from_secs(10));
        thread::sleep(Duration::from_millis(2600));
        assert!(!marker.exists(), "grandchild outlived the timeout");
    }

    #[test]
    fn grandchild_holding_the_pipe_is_bounded_by_the_timeout() {
        // The leader exits at once, but a background process keeps stdout open.
        let start = Instant::now();
        let r = sh("sleep 30 & echo leader-done; exit 0", 700);
        assert!(r.timed_out, "output never closed before the deadline");
        assert_eq!(r.exit_code, Some(0));
        assert_eq!(r.stdout, b"leader-done\n");
        assert!(start.elapsed() < Duration::from_secs(10));
    }

    #[test]
    fn stragglers_are_killed_after_a_normal_exit() {
        let dir = tempfile::tempdir().unwrap();
        let marker = dir.path().join("late");
        let script = format!(
            "(exec >/dev/null 2>&1; sleep 1; echo x > '{}') & exit 0",
            marker.display()
        );
        let r = sh(&script, 5000);
        assert_eq!(r.exit_code, Some(0));
        assert!(!r.timed_out);
        thread::sleep(Duration::from_millis(1500));
        assert!(!marker.exists(), "background process survived its step");
    }

    #[test]
    fn missing_executable_is_a_spawn_error() {
        let dir = tempfile::tempdir().unwrap();
        let e = run(&ProcSpec {
            argv: vec!["/nonexistent/argus-binary".into()],
            cwd: dir.path().to_path_buf(),
            env: vec![],
            stdin: None,
            timeout: Duration::from_secs(1),
        })
        .unwrap_err();
        assert!(e.0.contains("cannot start"));
    }

    #[test]
    fn large_output_is_capped() {
        let r = sh("head -c 5000000 /dev/zero", 10000);
        assert_eq!(r.exit_code, Some(0));
        assert_eq!(r.stdout.len(), MAX_CAPTURE);
    }
}
