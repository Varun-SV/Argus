//! Command line of the conformance runner. See `tests/conformance/README.md`.
//!
//! Exit codes: 0 when no case failed or errored, 1 when any did, 2 for usage or configuration
//! errors (no implementation to run, unreadable cases directory, unwritable report).

#![forbid(unsafe_code)]

use std::path::{Path, PathBuf};
use std::process::ExitCode;

use clap::{Parser, Subcommand, ValueEnum};

use argus_conformance::report::{self, REPORT_SCHEMA, Report, Summary};
use argus_conformance::runner::{self, Loaded, RunConfig};
use argus_conformance::shlex;

/// Environment variable naming the Python interpreter for `--impl python`.
const PYTHON_ENV: &str = "ARGUS_CONFORMANCE_PYTHON";

#[derive(Debug, Parser)]
#[command(
    name = "argus-conformance",
    version,
    about = "Argus black-box conformance runner"
)]
struct Cli {
    #[command(subcommand)]
    command: Cmd,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
enum Impl {
    /// `<python> -m argus`, interpreter from --python or $ARGUS_CONFORMANCE_PYTHON.
    Python,
    /// The Rust preview binary (default target/release/argus-next, or --bin).
    Rust,
    /// An explicit command line from --cmd.
    Custom,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, ValueEnum)]
enum Profile {
    Python,
    Rust,
}

#[derive(Debug, Subcommand)]
enum Cmd {
    /// Run the cases against one implementation.
    Run {
        #[arg(long = "impl", value_enum)]
        implementation: Impl,
        /// Command line for --impl custom, for example '/path/to/argus-next'.
        #[arg(long)]
        cmd: Option<String>,
        /// Which case statuses apply to --impl custom (default: rust).
        #[arg(long, value_enum)]
        profile: Option<Profile>,
        /// Python interpreter for --impl python (overrides $ARGUS_CONFORMANCE_PYTHON).
        #[arg(long)]
        python: Option<PathBuf>,
        /// Binary for --impl rust (default: <repo-root>/target/release/argus-next).
        #[arg(long)]
        bin: Option<PathBuf>,
        /// Directory of case files (searched recursively for *.json).
        #[arg(long, default_value = "tests/conformance/cases")]
        cases: PathBuf,
        /// Only run cases whose id or file path contains this text.
        #[arg(long)]
        filter: Option<String>,
        /// Write the JSON report here.
        #[arg(long)]
        report: Option<PathBuf>,
        /// Repository root (resolves the default --bin and `from` setup files).
        #[arg(long, default_value = ".")]
        repo_root: PathBuf,
        /// Parent directory for per-case sandboxes (default: the system temp directory).
        #[arg(long)]
        work_dir: Option<PathBuf>,
        /// Keep the sandbox of each failing or erroring case and print its path.
        #[arg(long)]
        keep_failed: bool,
        /// Also print notes and the reasons for pending and known-gap cases.
        #[arg(long, short)]
        verbose: bool,
    },
    /// Parse and validate every case file without running anything.
    Validate {
        #[arg(long, default_value = "tests/conformance/cases")]
        cases: PathBuf,
    },
}

fn absolute(p: &Path) -> Result<PathBuf, String> {
    std::path::absolute(p).map_err(|e| format!("cannot resolve {}: {e}", p.display()))
}

/// Resolve an executable given on the command line: a bare name is looked up on PATH by the
/// OS; anything with a directory part is made absolute, because steps run in a sandbox.
fn resolve_exe(p: &Path) -> Result<String, String> {
    if p.components().count() > 1 {
        let abs = absolute(p)?;
        if !abs.is_file() {
            return Err(format!("executable not found: {}", abs.display()));
        }
        Ok(abs.display().to_string())
    } else {
        Ok(p.display().to_string())
    }
}

struct Resolved {
    profile: &'static str,
    argv: Vec<String>,
}

fn resolve_impl(
    implementation: Impl,
    cmd: Option<String>,
    profile: Option<Profile>,
    python: Option<PathBuf>,
    bin: Option<PathBuf>,
    repo_root: &Path,
) -> Result<Resolved, String> {
    match implementation {
        Impl::Python => {
            let py = python
                .or_else(|| std::env::var_os(PYTHON_ENV).map(PathBuf::from))
                .ok_or_else(|| format!("--impl python needs --python or ${PYTHON_ENV}"))?;
            Ok(Resolved {
                profile: "python",
                argv: vec![resolve_exe(&py)?, "-m".into(), "argus".into()],
            })
        }
        Impl::Rust => {
            let exe = format!("argus-next{}", std::env::consts::EXE_SUFFIX);
            let bin = bin.unwrap_or_else(|| repo_root.join("target").join("release").join(exe));
            let resolved = resolve_exe(&bin).map_err(|e| {
                format!("{e} (build it with: cargo build --release -p argus-cli, or pass --bin)")
            })?;
            Ok(Resolved {
                profile: "rust",
                argv: vec![resolved],
            })
        }
        Impl::Custom => {
            let line = cmd.ok_or("--impl custom needs --cmd '<argv...>'")?;
            let mut argv = shlex::split(&line).map_err(|e| format!("--cmd: {e}"))?;
            if argv.is_empty() {
                return Err("--cmd is empty".into());
            }
            argv[0] = resolve_exe(Path::new(&argv[0]))?;
            let profile = match profile.unwrap_or(Profile::Rust) {
                Profile::Python => "python",
                Profile::Rust => "rust",
            };
            Ok(Resolved { profile, argv })
        }
    }
}

fn load(cases: &Path) -> Result<Vec<Loaded>, String> {
    if !cases.is_dir() {
        return Err(format!("cases directory not found: {}", cases.display()));
    }
    runner::load_cases(cases)
}

fn main() -> ExitCode {
    match real_main() {
        Ok(code) => ExitCode::from(code),
        Err(msg) => {
            eprintln!("argus-conformance: {msg}");
            ExitCode::from(2)
        }
    }
}

fn real_main() -> Result<u8, String> {
    match Cli::parse().command {
        Cmd::Validate { cases } => {
            let loaded = load(&cases)?;
            let mut bad = 0usize;
            for l in &loaded {
                match l {
                    Loaded::Ok { file, case } => println!("ok       {} ({file})", case.id),
                    Loaded::Invalid { file, error } => {
                        bad += 1;
                        println!("INVALID  {file}: {error}");
                    }
                }
            }
            println!("{} case files, {bad} invalid", loaded.len());
            Ok(u8::from(bad > 0))
        }
        Cmd::Run {
            implementation,
            cmd,
            profile,
            python,
            bin,
            cases,
            filter,
            report: report_path,
            repo_root,
            work_dir,
            keep_failed,
            verbose,
        } => {
            if profile.is_some() && implementation != Impl::Custom {
                return Err("--profile only applies to --impl custom".into());
            }
            let repo_root = absolute(&repo_root)?;
            let resolved = resolve_impl(implementation, cmd, profile, python, bin, &repo_root)?;
            let work_dir = work_dir.as_deref().map(absolute).transpose()?;
            let loaded = load(&cases)?;
            let cfg = RunConfig {
                profile: resolved.profile.into(),
                argv_prefix: resolved.argv.clone(),
                repo_root,
                work_dir,
                keep_failed,
                platform: std::env::consts::OS.into(),
            };
            let results = runner::run_all(&cfg, &loaded, filter.as_deref());
            let summary = Summary::of(&results);
            print!("{}", report::human(&results, verbose));
            if let Some(path) = report_path {
                let rep = Report {
                    schema: REPORT_SCHEMA,
                    implementation: resolved.profile,
                    command: report::display_command(&resolved.argv),
                    platform: std::env::consts::OS,
                    filter: filter.as_deref(),
                    summary,
                    results: &results,
                };
                let text = serde_json::to_string_pretty(&rep)
                    .map_err(|e| format!("cannot encode report: {e}"))?;
                std::fs::write(&path, text + "\n")
                    .map_err(|e| format!("cannot write report {}: {e}", path.display()))?;
                println!("report written to {}", path.display());
            }
            Ok(u8::try_from(summary.exit_code()).unwrap_or(1))
        }
    }
}
