//! `argus-bench`: benchmark harness for the performance budgets in
//! docs/rearchitecture/specification.md §11 (gate G-PERF). See bench/README.md.
//!
//! Exit codes: 0 when the result status is ok, skipped or pending; 1 when a run failed;
//! 2 for invalid arguments or scenario files.

#![forbid(unsafe_code)]

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Duration;

use argus_bench::attach::{AttachOptions, attach};
use argus_bench::process::SysinfoSource;
use argus_bench::report::{BenchResult, RESULT_SET_SCHEMA, ResultSet, Status, render_table};
use argus_bench::runner::{RunOptions, ToolPaths, run_scenario};
use argus_bench::scenario::{PLACEHOLDERS, Scenario};
use clap::{Args, Parser, Subcommand};

#[derive(Debug, Parser)]
#[command(
    name = "argus-bench",
    version,
    about = "Whole-process-tree memory and wall-time benchmarks (spec section 11, G-PERF).",
    after_help = "Scenario files and placeholders are documented in bench/README.md."
)]
struct Cli {
    #[command(subcommand)]
    command: Cmd,
}

#[derive(Debug, Subcommand)]
enum Cmd {
    /// Run a scenario file N times and report median, min and max.
    Run(RunArgs),
    /// Sample an already running process tree for a fixed time.
    Attach(AttachArgs),
    /// Parse and validate scenario files without running them.
    Check(CheckArgs),
    /// Combine result files into one result set (argus-bench-result-set-v1).
    Merge(MergeArgs),
    /// Print the table for a result or result-set JSON file.
    Show(ShowArgs),
}

#[derive(Debug, Args)]
struct OutputArgs {
    /// Sampling interval in milliseconds.
    #[arg(long, default_value_t = argus_bench::DEFAULT_INTERVAL_MS,
          value_parser = clap::value_parser!(u64).range(1..=60_000))]
    interval_ms: u64,
    /// Write the JSON result (schema argus-bench-result-v1) to this file.
    #[arg(long)]
    out: Option<PathBuf>,
    /// Print the JSON result on stdout instead of the table.
    #[arg(long)]
    json: bool,
    /// UTC timestamp to record (RFC 3339, e.g. 2026-10-08T12:00:00Z); default: the clock.
    #[arg(long)]
    timestamp: Option<String>,
    /// Free-form label recorded in the result, as key=value. Repeatable.
    #[arg(long = "label", value_name = "KEY=VALUE")]
    labels: Vec<String>,
}

#[derive(Debug, Args)]
struct RunArgs {
    /// Scenario file (TOML), e.g. bench/scenarios/python-help.toml.
    #[arg(long)]
    scenario: PathBuf,
    /// Counted runs (warm-up runs come on top).
    #[arg(long, default_value_t = argus_bench::DEFAULT_RUNS,
          value_parser = clap::value_parser!(u32).range(1..=1_000))]
    runs: u32,
    /// Override the scenario's warm-up run count.
    #[arg(long)]
    warmup: Option<u32>,
    /// Python interpreter for {PYTHON} (else ARGUS_BENCH_PYTHON, else python3/python).
    #[arg(long)]
    python: Option<String>,
    /// `argus` console script for {ARGUS} (else ARGUS_BENCH_ARGUS, else next to {PYTHON}).
    #[arg(long)]
    argus: Option<String>,
    /// `argus-gui` console script for {ARGUS_GUI}.
    #[arg(long)]
    argus_gui: Option<String>,
    /// Rust preview binary for {ARGUS_NEXT}.
    #[arg(long)]
    argus_next: Option<String>,
    /// Repository root for {REPO} (default: nearest ancestor of the scenario with bench/).
    #[arg(long)]
    root: Option<PathBuf>,
    /// Let the measured command print to this terminal (default: captured, tail shown on
    /// failure).
    #[arg(long)]
    show_output: bool,
    #[command(flatten)]
    output: OutputArgs,
}

#[derive(Debug, Args)]
struct AttachArgs {
    /// Root process id. Repeatable.
    #[arg(long = "pid", required_unless_present = "names")]
    pids: Vec<u32>,
    /// Root process name (case-insensitive, `.exe` optional); every top-most match becomes a
    /// root. Repeatable.
    #[arg(long = "name")]
    names: Vec<String>,
    /// How long to sample, e.g. 60s or 5m.
    #[arg(long, value_parser = humantime::parse_duration)]
    duration: Duration,
    /// Samples taken before this offset do not count towards peaks, e.g. 5s.
    #[arg(long, value_parser = humantime::parse_duration, default_value = "0s")]
    measure_after: Duration,
    #[command(flatten)]
    output: OutputArgs,
}

#[derive(Debug, Args)]
struct CheckArgs {
    /// Scenario files to validate.
    #[arg(required = true)]
    files: Vec<PathBuf>,
}

#[derive(Debug, Args)]
struct MergeArgs {
    /// Result files (argus-bench-result-v1) to combine, in order.
    #[arg(required = true)]
    files: Vec<PathBuf>,
    /// Description stored in the set.
    #[arg(long)]
    description: String,
    /// Output file.
    #[arg(long)]
    out: PathBuf,
}

#[derive(Debug, Args)]
struct ShowArgs {
    /// Result (argus-bench-result-v1) or result-set (argus-bench-result-set-v1) file.
    file: PathBuf,
}

fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T, String> {
    let bytes = std::fs::read(path).map_err(|e| format!("cannot read {}: {e}", path.display()))?;
    serde_json::from_slice(&bytes).map_err(|e| format!("{}: {e}", path.display()))
}

fn cmd_merge(args: &MergeArgs) -> Result<ExitCode, String> {
    let results = args
        .files
        .iter()
        .map(|f| read_json::<BenchResult>(f))
        .collect::<Result<Vec<_>, _>>()?;
    let set = ResultSet::from_results(&args.description, results)?;
    let json = serde_json::to_string_pretty(&set).map_err(|e| e.to_string())? + "\n";
    std::fs::write(&args.out, json)
        .map_err(|e| format!("cannot write {}: {e}", args.out.display()))?;
    out(&format!(
        "{} result(s) written to {}
",
        set.results.len(),
        args.out.display()
    ));
    Ok(ExitCode::SUCCESS)
}

fn cmd_show(args: &ShowArgs) -> Result<ExitCode, String> {
    let value: serde_json::Value = read_json(&args.file)?;
    if value["schema"] == RESULT_SET_SCHEMA {
        let set: ResultSet = read_json(&args.file)?;
        out(&format!("{}\n\n", set.description));
        for result in &set.results {
            out(&format!("{}\n", render_table(result)));
        }
    } else {
        let result: BenchResult = read_json(&args.file)?;
        out(&render_table(&result));
    }
    Ok(ExitCode::SUCCESS)
}

fn parse_labels(raw: &[String]) -> Result<BTreeMap<String, String>, String> {
    let mut labels = BTreeMap::new();
    for item in raw {
        let (key, value) = item
            .split_once('=')
            .filter(|(k, _)| !k.trim().is_empty())
            .ok_or_else(|| format!("label `{item}` must be key=value"))?;
        labels.insert(key.trim().to_owned(), value.to_owned());
    }
    Ok(labels)
}

fn emit(result: &BenchResult, output: &OutputArgs) -> Result<ExitCode, String> {
    let json = serde_json::to_string_pretty(result).map_err(|e| e.to_string())? + "\n";
    if let Some(path) = &output.out {
        std::fs::write(path, &json).map_err(|e| format!("cannot write {}: {e}", path.display()))?;
    }
    if output.json {
        out(&json);
    } else {
        out(&render_table(result));
        if let Some(path) = &output.out {
            out(&format!("\n  result written to {}\n", path.display()));
        }
    }
    Ok(match result.status {
        Status::Failed => ExitCode::from(1),
        Status::Ok | Status::Skipped | Status::Pending => ExitCode::SUCCESS,
    })
}

fn timestamp(output: &OutputArgs) -> Result<Option<String>, String> {
    output
        .timestamp
        .as_deref()
        .map(argus_bench::timestamp::normalize)
        .transpose()
}

fn cmd_run(args: RunArgs) -> Result<ExitCode, String> {
    let scenario = Scenario::load(&args.scenario).map_err(|e| e.to_string())?;
    let opts = RunOptions {
        runs: args.runs,
        interval: Duration::from_millis(args.output.interval_ms),
        warmup: args.warmup,
        timestamp: timestamp(&args.output)?,
        labels: parse_labels(&args.output.labels)?,
        tools: ToolPaths {
            python: args.python,
            argus: args.argus,
            argus_gui: args.argus_gui,
            argus_next: args.argus_next,
        },
        root: args.root,
        show_output: args.show_output,
    };
    let mut source = SysinfoSource::new();
    let result = run_scenario(&scenario, &args.scenario, &opts, &mut source)
        .map_err(|e| format!("{e:#}"))?;
    emit(&result, &args.output)
}

fn cmd_attach(args: AttachArgs) -> Result<ExitCode, String> {
    if args.duration.is_zero() {
        return Err("--duration must be greater than zero".to_owned());
    }
    let opts = AttachOptions {
        pids: args.pids,
        names: args.names,
        duration: args.duration,
        interval: Duration::from_millis(args.output.interval_ms),
        measure_after: args.measure_after,
        timestamp: timestamp(&args.output)?,
        labels: parse_labels(&args.output.labels)?,
    };
    let mut source = SysinfoSource::new();
    let result = attach(&opts, &mut source).map_err(|e| format!("{e:#}"))?;
    emit(&result, &args.output)
}

fn cmd_check(args: &CheckArgs) -> ExitCode {
    let mut failed = false;
    for file in &args.files {
        match Scenario::load(file) {
            Ok(s) => {
                let state = match &s.pending {
                    Some(p) => format!("pending: {p}"),
                    None => "runnable".to_owned(),
                };
                out(&format!("ok    {} ({}) {state}\n", display(file), s.name));
            }
            Err(e) => {
                failed = true;
                out(&format!("error {}: {e}\n", display(file)));
            }
        }
    }
    if failed {
        ExitCode::from(2)
    } else {
        ExitCode::SUCCESS
    }
}

/// Writes to stdout; a closed pipe (e.g. `| head`) ends the program quietly instead of
/// panicking.
fn out(text: &str) {
    use std::io::Write as _;
    let mut stdout = std::io::stdout().lock();
    if stdout
        .write_all(text.as_bytes())
        .and_then(|()| stdout.flush())
        .is_err()
    {
        std::process::exit(0);
    }
}

fn display(path: &Path) -> String {
    path.display().to_string()
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    let outcome = match cli.command {
        Cmd::Run(args) => cmd_run(args),
        Cmd::Attach(args) => cmd_attach(args),
        Cmd::Check(args) => Ok(cmd_check(&args)),
        Cmd::Merge(args) => cmd_merge(&args),
        Cmd::Show(args) => cmd_show(&args),
    };
    match outcome {
        Ok(code) => code,
        Err(message) => {
            eprintln!("argus-bench: {message}");
            if message.contains("placeholder") {
                eprintln!("known placeholders:");
                for (name, meaning) in PLACEHOLDERS {
                    eprintln!("  {{{name}}}  {meaning}");
                }
            }
            ExitCode::from(2)
        }
    }
}
