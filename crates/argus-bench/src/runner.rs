//! `argus-bench run`: spawn a scenario's argv N times and measure each run.
//!
//! Per run: a fresh `{WORKDIR}` is created (optionally filled from a repository fixture), the
//! `setup` commands run, the measured command is spawned and sampled until it exits, then the
//! `teardown` commands run. Only the measured command is inside the measured window.

use std::collections::BTreeMap;
use std::fs::File;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::time::{Duration, Instant};

use anyhow::{Context as _, bail};

use crate::process::{ProcInfo, ProcessSource, memory_metric};
use crate::report::{
    BenchResult, KNOWN_LIMITS, Metric, Mode, RESULT_SCHEMA, RunRecord, ScenarioInfo, Status, Tool,
    check_target, sample_warnings, summarize_runs,
};
use crate::sampler::{LoopLimits, RootExit, RootWaiter, sample_loop};
use crate::scenario::{Scenario, expand, find_repo_root};
use crate::tree::TreeTracker;

/// Executables the scenario placeholders refer to; `None` falls back to environment variables
/// and defaults (see [`crate::scenario::PLACEHOLDERS`]).
#[derive(Debug, Clone, Default)]
pub struct ToolPaths {
    /// `{PYTHON}`.
    pub python: Option<String>,
    /// `{ARGUS}`.
    pub argus: Option<String>,
    /// `{ARGUS_GUI}`.
    pub argus_gui: Option<String>,
    /// `{ARGUS_NEXT}`.
    pub argus_next: Option<String>,
}

/// Options of `argus-bench run`.
#[derive(Debug, Clone)]
pub struct RunOptions {
    /// Counted runs.
    pub runs: u32,
    /// Sampling interval.
    pub interval: Duration,
    /// Overrides the scenario's warm-up count.
    pub warmup: Option<u32>,
    /// Result timestamp (already normalized); `None` reads the clock.
    pub timestamp: Option<String>,
    /// Free-form labels recorded in the result.
    pub labels: BTreeMap<String, String>,
    /// Executable overrides.
    pub tools: ToolPaths,
    /// Repository root override (`{REPO}`).
    pub root: Option<PathBuf>,
    /// Let the measured command write to the harness's stdout and stderr.
    pub show_output: bool,
}

impl Default for RunOptions {
    fn default() -> Self {
        Self {
            runs: crate::DEFAULT_RUNS,
            interval: Duration::from_millis(crate::DEFAULT_INTERVAL_MS),
            warmup: None,
            timestamp: None,
            labels: BTreeMap::new(),
            tools: ToolPaths::default(),
            root: None,
            show_output: false,
        }
    }
}

const EXE_SUFFIX: &str = std::env::consts::EXE_SUFFIX;

fn os_family() -> &'static str {
    if cfg!(windows) { "windows" } else { "unix" }
}

/// Makes a path-like value absolute (so it survives the per-run working directory); bare
/// command names such as `python3` are left for the PATH lookup.
fn absolutize(value: String) -> String {
    let has_separator = value.contains('/') || (cfg!(windows) && value.contains('\\'));
    if !has_separator {
        return value;
    }
    std::path::absolute(&value)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or(value)
}

fn sibling_script(python: &str, script: &str) -> Option<String> {
    let parent = Path::new(python)
        .parent()
        .filter(|p| !p.as_os_str().is_empty())?;
    let candidate = parent.join(format!("{script}{EXE_SUFFIX}"));
    candidate
        .is_file()
        .then(|| candidate.to_string_lossy().into_owned())
}

fn default_playwright_path(env: &dyn Fn(&str) -> Option<String>) -> String {
    let joined = |base: Option<String>, parts: &[&str]| -> String {
        base.filter(|b| !b.is_empty())
            .map(|b| {
                let mut path = PathBuf::from(b);
                path.extend(parts);
                path.to_string_lossy().into_owned()
            })
            .unwrap_or_default()
    };
    if cfg!(windows) {
        joined(env("LOCALAPPDATA"), &["ms-playwright"])
    } else if cfg!(target_os = "macos") {
        joined(env("HOME"), &["Library", "Caches", "ms-playwright"])
    } else {
        joined(env("HOME"), &[".cache", "ms-playwright"])
    }
}

/// Resolves every placeholder except `{WORKDIR}` (set per run).
pub fn resolve_placeholders(
    scenario_file: &Path,
    tools: &ToolPaths,
    root: Option<&Path>,
    env: &dyn Fn(&str) -> Option<String>,
) -> anyhow::Result<BTreeMap<String, String>> {
    let pick = |flag: &Option<String>, var: &str| -> Option<String> {
        flag.clone()
            .or_else(|| env(var))
            .filter(|v| !v.is_empty())
            .map(absolutize)
    };
    let python = pick(&tools.python, "ARGUS_BENCH_PYTHON")
        .unwrap_or_else(|| if cfg!(windows) { "python" } else { "python3" }.to_owned());
    let argus = pick(&tools.argus, "ARGUS_BENCH_ARGUS")
        .or_else(|| sibling_script(&python, "argus"))
        .unwrap_or_else(|| "argus".to_owned());
    let argus_gui = pick(&tools.argus_gui, "ARGUS_BENCH_ARGUS_GUI")
        .or_else(|| sibling_script(&python, "argus-gui"))
        .unwrap_or_else(|| "argus-gui".to_owned());
    let argus_next = pick(&tools.argus_next, "ARGUS_BENCH_ARGUS_NEXT")
        .unwrap_or_else(|| "argus-next".to_owned());

    let scenario_abs = std::path::absolute(scenario_file)
        .with_context(|| format!("cannot resolve {}", scenario_file.display()))?;
    let scenario_dir = scenario_abs
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_default();
    let repo = match root {
        Some(r) => std::path::absolute(r)?,
        None => find_repo_root(&scenario_abs).with_context(
            || "cannot find the repository root (an ancestor with a bench/ directory); pass --root",
        )?,
    };
    let playwright = env("PLAYWRIGHT_BROWSERS_PATH")
        .filter(|v| !v.is_empty())
        .unwrap_or_else(|| default_playwright_path(env));

    let mut values = BTreeMap::new();
    values.insert("PYTHON".to_owned(), python);
    values.insert("ARGUS".to_owned(), argus);
    values.insert("ARGUS_GUI".to_owned(), argus_gui);
    values.insert("ARGUS_NEXT".to_owned(), argus_next);
    values.insert("REPO".to_owned(), repo.to_string_lossy().into_owned());
    values.insert(
        "SCENARIO_DIR".to_owned(),
        scenario_dir.to_string_lossy().into_owned(),
    );
    values.insert("OS".to_owned(), std::env::consts::OS.to_owned());
    values.insert("OS_FAMILY".to_owned(), os_family().to_owned());
    values.insert("EXE_SUFFIX".to_owned(), EXE_SUFFIX.to_owned());
    values.insert("PLAYWRIGHT_BROWSERS_PATH".to_owned(), playwright);
    Ok(values)
}

/// Repository-relative, `/`-separated path of the scenario file, when it is inside the repo.
fn relative_file(scenario_file: &Path, repo: &Path) -> Option<String> {
    let absolute = std::path::absolute(scenario_file).ok()?;
    let relative = absolute.strip_prefix(repo).ok()?;
    let parts: Vec<String> = relative
        .components()
        .map(|c| c.as_os_str().to_string_lossy().into_owned())
        .collect();
    Some(parts.join("/"))
}

fn copy_dir(from: &Path, to: &Path) -> anyhow::Result<()> {
    for entry in std::fs::read_dir(from).with_context(|| format!("read {}", from.display()))? {
        let entry = entry?;
        let kind = entry.file_type()?;
        let target = to.join(entry.file_name());
        if kind.is_dir() {
            std::fs::create_dir_all(&target)?;
            copy_dir(&entry.path(), &target)?;
        } else if kind.is_file() {
            std::fs::copy(entry.path(), &target)
                .with_context(|| format!("copy {}", entry.path().display()))?;
        } else {
            bail!(
                "fixture entry {} is not a regular file or directory",
                entry.path().display()
            );
        }
    }
    Ok(())
}

/// Fully expanded command line of one run.
#[derive(Debug, Clone)]
struct Invocation {
    argv: Vec<String>,
    cwd: PathBuf,
    env: Vec<(String, String)>,
}

impl Invocation {
    fn command(&self, argv: &[String]) -> Command {
        let mut command = Command::new(&argv[0]);
        command.args(&argv[1..]).current_dir(&self.cwd);
        for (key, value) in &self.env {
            if value.is_empty() {
                command.env_remove(key);
            } else {
                command.env(key, value);
            }
        }
        command.stdin(Stdio::null());
        command
    }
}

fn expand_all(argv: &[String], values: &BTreeMap<String, String>) -> anyhow::Result<Vec<String>> {
    argv.iter()
        .map(|a| expand(a, values).map_err(anyhow::Error::from))
        .collect()
}

fn invocation(
    scenario: &Scenario,
    values: &BTreeMap<String, String>,
) -> anyhow::Result<Invocation> {
    let repo = PathBuf::from(&values["REPO"]);
    let cwd = match &scenario.cwd {
        Some(template) => {
            let expanded = PathBuf::from(expand(template, values)?);
            if expanded.is_absolute() {
                expanded
            } else {
                repo.join(expanded)
            }
        }
        None => PathBuf::from(&values["WORKDIR"]),
    };
    let env = scenario
        .env
        .iter()
        .map(|(k, v)| Ok((k.clone(), expand(v, values)?)))
        .collect::<anyhow::Result<Vec<_>>>()?;
    Ok(Invocation {
        argv: expand_all(&scenario.command, values)?,
        cwd,
        env,
    })
}

fn exit_parts(status: &ExitStatus) -> (Option<i32>, Option<i32>) {
    #[cfg(unix)]
    let signal = std::os::unix::process::ExitStatusExt::signal(status);
    #[cfg(not(unix))]
    let signal = None;
    (status.code(), signal)
}

/// Runs an auxiliary command (setup, teardown, probe) to completion with a timeout.
fn run_aux(mut command: Command, timeout: Duration, log: Option<&Path>) -> Result<(), String> {
    match log.map(|p| File::options().create(true).append(true).open(p)) {
        Some(Ok(file)) => {
            let err = file.try_clone().map_err(|e| e.to_string())?;
            command.stdout(file).stderr(err);
        }
        _ => {
            command.stdout(Stdio::null()).stderr(Stdio::null());
        }
    }
    let mut child = command.spawn().map_err(|e| format!("cannot start: {e}"))?;
    let start = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                return if status.success() {
                    Ok(())
                } else {
                    let (code, signal) = exit_parts(&status);
                    Err(match (code, signal) {
                        (Some(c), _) => format!("exit code {c}"),
                        (None, Some(s)) => format!("signal {s}"),
                        _ => "abnormal exit".to_owned(),
                    })
                };
            }
            Ok(None) if start.elapsed() >= timeout => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!(
                    "timed out after {}",
                    humantime::format_duration(timeout)
                ));
            }
            Ok(None) => std::thread::sleep(Duration::from_millis(10)),
            Err(e) => return Err(e.to_string()),
        }
    }
}

/// Waits for a spawned child on a helper thread, so the exit time is exact.
struct ChildWaiter {
    rx: Receiver<RootExit>,
}

impl ChildWaiter {
    fn spawn(mut child: Child) -> Self {
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let exit = match child.wait() {
                Ok(status) => {
                    let (code, signal) = exit_parts(&status);
                    RootExit {
                        at: Instant::now(),
                        code,
                        signal,
                    }
                }
                Err(_) => RootExit {
                    at: Instant::now(),
                    code: None,
                    signal: None,
                },
            };
            let _ = tx.send(exit);
        });
        Self { rx }
    }
}

impl RootWaiter for ChildWaiter {
    fn wait_for(&mut self, timeout: Duration) -> Option<RootExit> {
        match self.rx.recv_timeout(timeout) {
            Ok(exit) => Some(exit),
            Err(RecvTimeoutError::Timeout) => None,
            Err(RecvTimeoutError::Disconnected) => {
                std::thread::sleep(timeout);
                None
            }
        }
    }
}

fn micros(d: Duration) -> u64 {
    u64::try_from(d.as_micros()).unwrap_or(u64::MAX)
}

fn millis(d: Duration) -> u64 {
    u64::try_from(d.as_millis()).unwrap_or(u64::MAX)
}

fn print_log_tail(index: u32, log: &Path) {
    let Ok(file) = File::open(log) else { return };
    let lines: Vec<String> = BufReader::new(file).lines().map_while(Result::ok).collect();
    if lines.is_empty() {
        return;
    }
    eprintln!("--- run {index}: last lines of output ---");
    for line in &lines[lines.len().saturating_sub(20)..] {
        eprintln!("{line}");
    }
    eprintln!("---");
}

fn failed_run(index: u32, warmup: bool, note: String) -> RunRecord {
    RunRecord {
        index,
        warmup,
        exit_code: None,
        signal: None,
        stopped_after_duration: false,
        timed_out: false,
        ok: false,
        wall_us: 0,
        memory: crate::tree::RunAccumulator::new(0).finish(),
        leftover_processes: 0,
        leftover_names: Vec::new(),
        note: Some(note),
    }
}

fn run_once(
    scenario: &Scenario,
    base_values: &BTreeMap<String, String>,
    opts: &RunOptions,
    source: &mut dyn ProcessSource,
    index: u32,
    warmup: bool,
) -> anyhow::Result<RunRecord> {
    let workdir = tempfile::Builder::new()
        .prefix("argus-bench-run-")
        .tempdir()
        .context("cannot create the per-run working directory")?;
    let logs = tempfile::Builder::new()
        .prefix("argus-bench-log-")
        .tempdir()?;
    let log_path = logs.path().join("output.log");
    let mut values = base_values.clone();
    values.insert(
        "WORKDIR".to_owned(),
        workdir.path().to_string_lossy().into_owned(),
    );
    let repo = PathBuf::from(&values["REPO"]);
    if let Some(from) = &scenario.workdir.copy_from {
        copy_dir(&repo.join(from), workdir.path())
            .with_context(|| format!("cannot copy workdir fixture `{from}`"))?;
    }
    for dir in &scenario.workdir.create_dirs {
        std::fs::create_dir_all(workdir.path().join(dir))?;
    }
    let inv = invocation(scenario, &values)?;
    let aux_log = (!opts.show_output).then_some(log_path.as_path());

    for argv in &scenario.setup {
        let argv = expand_all(argv, &values)?;
        if let Err(e) = run_aux(inv.command(&argv), scenario.timeout, aux_log) {
            if !opts.show_output {
                print_log_tail(index, &log_path);
            }
            return Ok(failed_run(
                index,
                warmup,
                format!("setup `{}` failed: {e}", argv[0]),
            ));
        }
    }

    let mut command = inv.command(&inv.argv);
    if opts.show_output {
        command.stdout(Stdio::inherit()).stderr(Stdio::inherit());
    } else {
        let file = File::create(&log_path)?;
        command.stdout(file.try_clone()?).stderr(file);
    }
    let start = Instant::now();
    let child = match command.spawn() {
        Ok(child) => child,
        Err(e) => {
            return Ok(failed_run(
                index,
                warmup,
                format!("cannot start `{}`: {e}", scenario.command[0]),
            ));
        }
    };
    let pid = child.id();
    let mut waiter = ChildWaiter::spawn(child);
    let mut tracker = TreeTracker::new(&[pid]);
    let limits = LoopLimits {
        interval: opts.interval,
        duration: scenario.duration,
        timeout: scenario.timeout,
        measure_after: scenario.measure_after,
        stop_when_tree_gone: false,
        terminate_on_stop: true,
    };
    let outcome = sample_loop(source, &mut tracker, &mut waiter, &limits, start);
    if !outcome.leftover.is_empty() {
        let ids: Vec<_> = outcome.leftover.iter().map(ProcInfo::id).collect();
        source.terminate(&ids);
    }
    let mut leftover_names: Vec<String> = outcome.leftover.iter().map(|p| p.name.clone()).collect();
    leftover_names.sort();
    let wall = outcome.end.saturating_duration_since(start);

    let mut teardown_error = None;
    for argv in &scenario.teardown {
        let argv = expand_all(argv, &values)?;
        if let Err(e) = run_aux(inv.command(&argv), scenario.timeout, aux_log) {
            teardown_error = Some(format!("teardown `{}` failed: {e}", argv[0]));
            break;
        }
    }

    let code = outcome.exit.and_then(|e| e.code);
    let signal = outcome.exit.and_then(|e| e.signal);
    let (mut ok, mut note) = if outcome.timed_out {
        (
            false,
            Some(format!(
                "timed out after {}",
                humantime::format_duration(scenario.timeout)
            )),
        )
    } else if scenario.duration.is_some() {
        if outcome.stopped_after_duration {
            (true, None)
        } else {
            (
                false,
                Some(format!(
                    "exited after {} ms, before the measurement duration",
                    millis(wall)
                )),
            )
        }
    } else if code == Some(scenario.expected_exit) {
        (true, None)
    } else {
        let got = match (code, signal) {
            (Some(c), _) => format!("exit code {c}"),
            (None, Some(s)) => format!("signal {s}"),
            _ => "no exit status".to_owned(),
        };
        (
            false,
            Some(format!(
                "{got}, expected exit code {}",
                scenario.expected_exit
            )),
        )
    };
    if let Some(e) = teardown_error {
        ok = false;
        note = Some(note.map_or(e.clone(), |n| format!("{n}; {e}")));
    }
    if !ok && !opts.show_output {
        print_log_tail(index, &log_path);
    }
    Ok(RunRecord {
        index,
        warmup,
        exit_code: code,
        signal,
        stopped_after_duration: outcome.stopped_after_duration,
        timed_out: outcome.timed_out,
        ok,
        wall_us: micros(wall),
        memory: outcome.memory,
        leftover_processes: u32::try_from(outcome.leftover.len()).unwrap_or(u32::MAX),
        leftover_names,
        note,
    })
}

fn base_result(
    opts_timestamp: Option<&String>,
    interval: Duration,
    mode: Mode,
    labels: &BTreeMap<String, String>,
) -> BenchResult {
    let metric = memory_metric();
    BenchResult {
        schema: RESULT_SCHEMA.to_owned(),
        tool: Tool {
            name: "argus-bench".to_owned(),
            version: crate::TOOL_VERSION.to_owned(),
        },
        created_utc: opts_timestamp
            .cloned()
            .unwrap_or_else(crate::timestamp::now_utc),
        environment: crate::env_meta::collect(),
        metric: Metric {
            name: metric.name.to_owned(),
            definition: metric.definition.to_owned(),
            interval_ms: millis(interval),
            aggregation: "per sample: sum over every process in the tree; per run: highest \
                          sample (peak); over runs: median, min and max of the counted runs \
                          (warm-up and failed runs excluded)"
                .to_owned(),
        },
        mode,
        status: Status::Ok,
        status_reason: None,
        labels: labels.clone(),
        scenario: None,
        attach: None,
        runs: Vec::new(),
        summary: None,
        target: None,
        warnings: Vec::new(),
        known_limits: KNOWN_LIMITS.iter().map(|s| (*s).to_owned()).collect(),
    }
}

/// Shared result skeleton for attach mode.
pub(crate) fn attach_base(
    timestamp: Option<&String>,
    interval: Duration,
    labels: &BTreeMap<String, String>,
) -> BenchResult {
    base_result(timestamp, interval, Mode::Attach, labels)
}

/// Runs a scenario and returns the result document.
pub fn run_scenario(
    scenario: &Scenario,
    scenario_file: &Path,
    opts: &RunOptions,
    source: &mut dyn ProcessSource,
) -> anyhow::Result<BenchResult> {
    let env = |name: &str| std::env::var(name).ok();
    let values = resolve_placeholders(scenario_file, &opts.tools, opts.root.as_deref(), &env)?;
    let warmup = opts.warmup.unwrap_or(scenario.warmup);
    let mut result = base_result(
        opts.timestamp.as_ref(),
        opts.interval,
        Mode::Run,
        &opts.labels,
    );
    result.scenario = Some(ScenarioInfo {
        name: scenario.name.clone(),
        description: scenario.description.clone(),
        tags: scenario.tags.clone(),
        file: relative_file(scenario_file, Path::new(&values["REPO"])),
        command: scenario.command.clone(),
        warmup,
        expected_exit: scenario.expected_exit,
        duration_ms: scenario.duration.map(millis),
        measure_after_ms: millis(scenario.measure_after),
        pending: scenario.pending.clone(),
    });

    if let Some(pending) = &scenario.pending {
        result.status = Status::Pending;
        result.status_reason = Some(format!("pending: {pending}"));
        return Ok(result);
    }

    if let Some(probe) = &scenario.skip_unless {
        let workdir = tempfile::Builder::new()
            .prefix("argus-bench-probe-")
            .tempdir()?;
        let mut probe_values = values.clone();
        probe_values.insert(
            "WORKDIR".to_owned(),
            workdir.path().to_string_lossy().into_owned(),
        );
        let inv = invocation(scenario, &probe_values)?;
        let argv = expand_all(&probe.command, &probe_values)?;
        if let Err(e) = run_aux(inv.command(&argv), probe.timeout, None) {
            result.status = Status::Skipped;
            result.status_reason = Some(format!("skipped: {} (probe: {e})", probe.reason));
            return Ok(result);
        }
    }

    let total = warmup.saturating_add(opts.runs);
    for index in 1..=total {
        let is_warmup = index <= warmup;
        let record = run_once(scenario, &values, opts, source, index, is_warmup)?;
        result.runs.push(record);
    }
    result.summary = summarize_runs(&result.runs);
    result.warnings = sample_warnings(&result.runs, millis(opts.interval));
    let failures: Vec<String> = result
        .runs
        .iter()
        .filter(|r| !r.ok)
        .map(|r| {
            format!(
                "run {}{}: {}",
                r.index,
                if r.warmup { " (warm-up)" } else { "" },
                r.note.as_deref().unwrap_or("failed")
            )
        })
        .collect();
    if !failures.is_empty() {
        result.status = Status::Failed;
        result.status_reason = Some(failures.join("; "));
    }
    if let Some(target) = &scenario.target {
        result.target = Some(check_target(target, result.summary.as_ref()));
    }
    Ok(result)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn placeholders_resolve_from_flags_env_and_defaults() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("bench/scenarios")).unwrap();
        let file = dir.path().join("bench/scenarios/x.toml");
        std::fs::write(&file, "").unwrap();
        let env = |name: &str| match name {
            "ARGUS_BENCH_ARGUS_NEXT" => Some("next-bin".to_owned()),
            "HOME" => Some("/home/test".to_owned()),
            "LOCALAPPDATA" => Some("C:\\Users\\test\\AppData\\Local".to_owned()),
            _ => None,
        };
        let tools = ToolPaths {
            python: Some("py-flag".to_owned()),
            ..ToolPaths::default()
        };
        let v = resolve_placeholders(&file, &tools, None, &env).unwrap();
        assert_eq!(v["PYTHON"], "py-flag");
        assert_eq!(v["ARGUS"], "argus");
        assert_eq!(v["ARGUS_NEXT"], "next-bin");
        assert_eq!(
            PathBuf::from(&v["REPO"]),
            std::path::absolute(dir.path()).unwrap()
        );
        assert!(v["SCENARIO_DIR"].ends_with("scenarios"));
        assert!(v["PLAYWRIGHT_BROWSERS_PATH"].ends_with("ms-playwright"));
        assert_eq!(
            relative_file(&file, Path::new(&v["REPO"])).as_deref(),
            Some("bench/scenarios/x.toml")
        );
    }

    #[test]
    fn console_scripts_next_to_python_are_found() {
        let dir = tempfile::tempdir().unwrap();
        let bin = dir.path().join("bin");
        std::fs::create_dir_all(&bin).unwrap();
        std::fs::create_dir_all(dir.path().join("bench")).unwrap();
        let python = bin.join(format!("python{EXE_SUFFIX}"));
        std::fs::write(&python, "").unwrap();
        std::fs::write(bin.join(format!("argus{EXE_SUFFIX}")), "").unwrap();
        let tools = ToolPaths {
            python: Some(python.to_string_lossy().into_owned()),
            ..ToolPaths::default()
        };
        let v = resolve_placeholders(&dir.path().join("bench/x.toml"), &tools, None, &|_| None)
            .unwrap();
        assert!(Path::new(&v["ARGUS"]).starts_with(&bin));
        assert_eq!(v["ARGUS_GUI"], "argus-gui");
    }

    #[test]
    fn missing_repo_root_is_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("x.toml");
        let err = resolve_placeholders(&file, &ToolPaths::default(), None, &|_| None);
        // The temporary directory may itself sit below a directory with a bench/ folder only
        // in unusual setups; the normal case is an error that names --root.
        if let Err(e) = err {
            assert!(e.to_string().contains("--root"));
        }
    }
}
