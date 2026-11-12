//! Loading case files and running them against one implementation.

use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use serde::Serialize;

use crate::assertions::{self, Observed};
use crate::case::{self, Case, CaseStatus};
use crate::normalize::Normalizer;
use crate::process::{self, ProcSpec};
use crate::sandbox::Sandbox;

/// Bytes of normalized output kept per stream in the report.
pub const REPORT_OUTPUT_LIMIT: usize = 16 * 1024;

/// A case file after loading: either a valid case or the reason it is not one.
#[derive(Debug, Clone)]
pub enum Loaded {
    Ok { file: String, case: Box<Case> },
    Invalid { file: String, error: String },
}

impl Loaded {
    /// The sort and filter key: the case id, or the file for an invalid file.
    pub fn key(&self) -> &str {
        match self {
            Self::Ok { case, .. } => &case.id,
            Self::Invalid { file, .. } => file,
        }
    }
}

fn collect_json(dir: &Path, out: &mut Vec<PathBuf>) -> std::io::Result<()> {
    for entry in fs::read_dir(dir)? {
        let path = entry?.path();
        if path.is_dir() {
            collect_json(&path, out)?;
        } else if path.extension().is_some_and(|e| e == "json") {
            out.push(path);
        }
    }
    Ok(())
}

/// Load every `*.json` below `dir`, sorted by id. Duplicate ids make both files invalid.
pub fn load_cases(dir: &Path) -> Result<Vec<Loaded>, String> {
    let mut files = Vec::new();
    collect_json(dir, &mut files).map_err(|e| format!("cannot read cases dir {dir:?}: {e}"))?;
    files.sort();
    let mut loaded = Vec::new();
    for path in files {
        let rel = path
            .strip_prefix(dir)
            .unwrap_or(&path)
            .to_string_lossy()
            .replace('\\', "/");
        let item = match fs::read_to_string(&path) {
            Err(e) => Loaded::Invalid {
                file: rel,
                error: format!("cannot read: {e}"),
            },
            Ok(text) => match case::parse_case(&text) {
                Ok(c) => Loaded::Ok {
                    file: rel,
                    case: Box::new(c),
                },
                Err(e) => Loaded::Invalid {
                    file: rel,
                    error: e.message,
                },
            },
        };
        loaded.push(item);
    }
    let mut seen: BTreeMap<String, usize> = BTreeMap::new();
    for l in &loaded {
        if let Loaded::Ok { case, .. } = l {
            *seen.entry(case.id.clone()).or_default() += 1;
        }
    }
    let loaded = loaded
        .into_iter()
        .map(|l| match l {
            Loaded::Ok { file, case } if seen[&case.id] > 1 => Loaded::Invalid {
                file,
                error: format!("duplicate case id {:?}", case.id),
            },
            other => other,
        })
        .collect::<Vec<_>>();
    let mut loaded = loaded;
    loaded.sort_by(|a, b| a.key().cmp(b.key()));
    Ok(loaded)
}

/// Final classification of one case.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Outcome {
    Pass,
    Fail,
    Pending,
    KnownGap,
    Skipped,
    Error,
}

impl Outcome {
    pub fn label(self) -> &'static str {
        match self {
            Self::Pass => "PASS",
            Self::Fail => "FAIL",
            Self::Pending => "PENDING",
            Self::KnownGap => "KNOWN-GAP",
            Self::Skipped => "SKIP",
            Self::Error => "ERROR",
        }
    }
}

/// Report entry for one step.
#[derive(Debug, Clone, Serialize)]
pub struct StepReport {
    pub index: usize,
    pub args: Vec<String>,
    pub exit_code: Option<i32>,
    pub timed_out: bool,
    pub duration_ms: u128,
    pub failures: Vec<String>,
    pub stdout: String,
    pub stderr: String,
}

/// Report entry for one case.
#[derive(Debug, Clone, Serialize)]
pub struct CaseResult {
    pub id: String,
    pub file: String,
    pub title: String,
    pub requirements: Vec<String>,
    /// The status the case declares for this implementation (`none` when not listed).
    pub declared: String,
    pub outcome: Outcome,
    /// For pending and known-gap cases: whether every assertion held (the case can be
    /// promoted to active).
    #[serde(skip_serializing_if = "Option::is_none")]
    pub would_pass: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub note: Option<String>,
    pub failures: Vec<String>,
    pub steps: Vec<StepReport>,
    pub duration_ms: u128,
}

/// How to run the implementation under test.
#[derive(Debug, Clone)]
pub struct RunConfig {
    /// Which `impls` entry applies: `python` or `rust`.
    pub profile: String,
    /// The executable and fixed leading arguments (for Python: `<python> -m argus`).
    pub argv_prefix: Vec<String>,
    /// Repository root, used to resolve `from` setup files.
    pub repo_root: PathBuf,
    /// Parent directory for sandboxes (system temp dir when `None`).
    pub work_dir: Option<PathBuf>,
    /// Keep sandboxes of failing or erroring cases for inspection.
    pub keep_failed: bool,
    /// Operating system name as in `std::env::consts::OS`.
    pub platform: String,
}

fn truncate(mut s: String, limit: usize) -> String {
    if s.len() > limit {
        let mut cut = limit;
        while !s.is_char_boundary(cut) {
            cut -= 1;
        }
        s.truncate(cut);
        s.push_str("\n...[truncated]");
    }
    s
}

fn skipped(file: &str, case: &Case, declared: String, why: String) -> CaseResult {
    CaseResult {
        id: case.id.clone(),
        file: file.to_owned(),
        title: case.title.clone(),
        requirements: case.requirements.clone(),
        declared,
        outcome: Outcome::Skipped,
        would_pass: None,
        note: Some(why),
        failures: vec![],
        steps: vec![],
        duration_ms: 0,
    }
}

/// Run one loaded case.
pub fn run_one(cfg: &RunConfig, loaded: &Loaded) -> CaseResult {
    let (file, case) = match loaded {
        Loaded::Invalid { file, error } => {
            return CaseResult {
                id: file.clone(),
                file: file.clone(),
                title: String::new(),
                requirements: vec![],
                declared: "invalid".into(),
                outcome: Outcome::Error,
                would_pass: None,
                note: None,
                failures: vec![format!("invalid case file: {error}")],
                steps: vec![],
                duration_ms: 0,
            };
        }
        Loaded::Ok { file, case } => (file.as_str(), case.as_ref()),
    };
    let Some(entry) = case.impls.get(&cfg.profile) else {
        return skipped(
            file,
            case,
            "none".into(),
            format!("not applicable to {}", cfg.profile),
        );
    };
    let declared = entry.status.to_string();
    if entry.status == CaseStatus::Skip {
        let why = entry
            .note
            .clone()
            .unwrap_or_else(|| "skipped for this implementation".into());
        return skipped(file, case, declared, why);
    }
    if !case.platforms.is_empty() && !case.platforms.contains(&cfg.platform) {
        return skipped(
            file,
            case,
            declared,
            format!("not applicable on {}", cfg.platform),
        );
    }

    let start = Instant::now();
    let mut result = CaseResult {
        id: case.id.clone(),
        file: file.to_owned(),
        title: case.title.clone(),
        requirements: case.requirements.clone(),
        declared,
        outcome: Outcome::Pass,
        would_pass: None,
        note: entry.note.clone().or_else(|| case.note.clone()),
        failures: vec![],
        steps: vec![],
        duration_ms: 0,
    };
    let errored = run_steps(cfg, case, &mut result);
    let all_held = !errored && result.failures.is_empty();
    result.outcome = match (errored, entry.status) {
        (true, _) => Outcome::Error,
        (false, CaseStatus::Pending) => Outcome::Pending,
        (false, CaseStatus::KnownGap) => Outcome::KnownGap,
        (false, _) if all_held => Outcome::Pass,
        (false, _) => Outcome::Fail,
    };
    if matches!(result.outcome, Outcome::Pending | Outcome::KnownGap) {
        result.would_pass = Some(all_held);
    }
    result.duration_ms = start.elapsed().as_millis();
    result
}

/// Run the steps; failures go into `result`. Returns true on a runner error (setup, spawn).
fn run_steps(cfg: &RunConfig, case: &Case, result: &mut CaseResult) -> bool {
    let sb = match Sandbox::create(cfg.work_dir.as_deref()) {
        Ok(sb) => sb,
        Err(e) => {
            result.failures.push(format!("cannot create sandbox: {e}"));
            return true;
        }
    };
    let mut errored = false;
    if let Err(e) = sb.apply_setup(&case.setup, &cfg.repo_root) {
        result.failures.push(e);
        errored = true;
    }
    let norm = match Normalizer::new(&case.normalizers, &sb.path_spellings()) {
        Ok(n) => Some(n),
        Err(e) => {
            result.failures.push(e);
            errored = true;
            None
        }
    };
    if let (false, Some(norm)) = (errored, norm) {
        let base_env = sb.base_env();
        for (index, step) in case.steps.iter().enumerate() {
            let mut env = base_env.clone();
            for (k, v) in &step.env {
                env.retain(|(name, _)| name != k);
                env.push((k.clone(), sb.expand(v)));
            }
            let mut argv = cfg.argv_prefix.clone();
            argv.extend(step.args.iter().map(|a| sb.expand(a)));
            let spec = ProcSpec {
                argv,
                cwd: sb.project(),
                env,
                stdin: step.stdin.as_ref().map(|s| sb.expand(s).into_bytes()),
                timeout: Duration::from_secs_f64(step.timeout()),
            };
            let proc = match process::run(&spec) {
                Ok(p) => p,
                Err(e) => {
                    result.failures.push(format!("step {index}: {e}"));
                    errored = true;
                    break;
                }
            };
            let obs = Observed {
                exit_code: proc.exit_code,
                timed_out: proc.timed_out,
                stdout: norm.apply(&proc.stdout),
                stderr: norm.apply(&proc.stderr),
            };
            let failures = assertions::check_step(&sb, &step.expect, &obs);
            result
                .failures
                .extend(failures.iter().map(|f| format!("step {index}: {f}")));
            result.steps.push(StepReport {
                index,
                args: step.args.clone(),
                exit_code: proc.exit_code,
                timed_out: proc.timed_out,
                duration_ms: proc.duration.as_millis(),
                failures: failures.clone(),
                stdout: truncate(obs.stdout, REPORT_OUTPUT_LIMIT),
                stderr: truncate(obs.stderr, REPORT_OUTPUT_LIMIT),
            });
            if !failures.is_empty() {
                // Later steps build on earlier ones; their results would only be noise.
                let left = case.steps.len() - index - 1;
                if left > 0 {
                    result
                        .failures
                        .push(format!("{left} later step(s) not run"));
                }
                break;
            }
        }
    }
    if cfg.keep_failed && (errored || !result.failures.is_empty()) {
        let kept = sb.keep();
        result
            .failures
            .push(format!("sandbox kept at {}", kept.display()));
    }
    errored
}

/// Run every loaded case whose id or file contains `filter`, in id order.
pub fn run_all(cfg: &RunConfig, cases: &[Loaded], filter: Option<&str>) -> Vec<CaseResult> {
    cases
        .iter()
        .filter(|l| match (filter, l) {
            (None, _) => true,
            (Some(f), Loaded::Ok { file, case }) => case.id.contains(f) || file.contains(f),
            (Some(f), Loaded::Invalid { file, .. }) => file.contains(f),
        })
        .map(|l| run_one(cfg, l))
        .collect()
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;

    fn cfg(profile: &str) -> RunConfig {
        RunConfig {
            profile: profile.into(),
            argv_prefix: vec!["/bin/sh".into(), "-c".into()],
            repo_root: PathBuf::from("."),
            work_dir: None,
            keep_failed: false,
            platform: std::env::consts::OS.into(),
        }
    }

    fn load(json: &str) -> Loaded {
        Loaded::Ok {
            file: "t.json".into(),
            case: Box::new(case::parse_case(json).expect("valid test case")),
        }
    }

    const CASE: &str = r#"{"schema":"argus-conformance-case-v1","id":"t.echo","title":"t",
        "impls":{"python":{"status":"active"},"rust":{"status":"pending","note":"later"}},
        "setup":{"files":[{"path":"in.txt","content":"hello"}]},
        "steps":[
          {"args":["cat in.txt; pwd; echo \"$HOME\" >&2; exit 3"],
           "expect":{"exit":3,"stdout":{"contains":["hello","<TMP>/project"]},
                     "stderr":{"regex":["^<TMP>/home"]}}},
          {"args":["echo \"$ARGUS_X\" > out.json"],"env":{"ARGUS_X":"{\"k\":1}"},
           "expect":{"exit":0,"json":[{"file":"out.json","path":"$.k","equals":1}]}}
        ]}"#;

    #[test]
    fn active_case_passes_with_setup_env_placeholders_and_normalization() {
        let r = run_one(&cfg("python"), &load(CASE));
        assert_eq!(r.outcome, Outcome::Pass, "{:?}", r.failures);
        assert_eq!(r.steps.len(), 2);
        assert!(r.steps[0].stdout.contains("<TMP>/project"));
    }

    #[test]
    fn pending_case_is_run_but_not_failed() {
        let r = run_one(&cfg("rust"), &load(CASE));
        assert_eq!(r.outcome, Outcome::Pending);
        assert_eq!(r.would_pass, Some(true));
        let failing = CASE.replace("\"exit\":3", "\"exit\":4");
        let r = run_one(&cfg("rust"), &load(&failing));
        assert_eq!(r.outcome, Outcome::Pending);
        assert_eq!(r.would_pass, Some(false));
        let r = run_one(&cfg("python"), &load(&failing));
        assert_eq!(r.outcome, Outcome::Fail);
        assert!(
            r.failures
                .iter()
                .any(|f| f.contains("1 later step(s) not run")),
            "{:?}",
            r.failures
        );
    }

    #[test]
    fn unlisted_impl_platform_and_skip_are_skipped() {
        let only_rust = CASE.replace(r#""python":{"status":"active"},"#, "");
        assert_eq!(
            run_one(&cfg("python"), &load(&only_rust)).outcome,
            Outcome::Skipped
        );
        let other_os = if cfg!(target_os = "linux") {
            "windows"
        } else {
            "linux"
        };
        let plat = CASE.replace(
            "\"setup\"",
            &format!("\"platforms\":[\"{other_os}\"],\"setup\""),
        );
        assert_eq!(
            run_one(&cfg("python"), &load(&plat)).outcome,
            Outcome::Skipped
        );
    }

    #[test]
    fn spawn_failure_and_invalid_files_are_errors() {
        let mut c = cfg("python");
        c.argv_prefix = vec!["/nonexistent/argus".into()];
        assert_eq!(run_one(&c, &load(CASE)).outcome, Outcome::Error);
        let bad = Loaded::Invalid {
            file: "x.json".into(),
            error: "boom".into(),
        };
        assert_eq!(run_one(&cfg("python"), &bad).outcome, Outcome::Error);
    }

    #[test]
    fn known_gap_reports_whether_it_now_passes() {
        let gap = CASE.replace(
            r#""python":{"status":"active"}"#,
            r#""python":{"status":"known-gap","note":"defect"}"#,
        );
        let r = run_one(&cfg("python"), &load(&gap));
        assert_eq!((r.outcome, r.would_pass), (Outcome::KnownGap, Some(true)));
    }

    #[test]
    fn load_cases_sorts_and_flags_duplicates_and_bad_files() {
        let dir = tempfile::tempdir().unwrap();
        fs::create_dir_all(dir.path().join("cli")).unwrap();
        fs::write(dir.path().join("cli/b.json"), CASE).unwrap();
        fs::write(dir.path().join("a.json"), CASE.replace("t.echo", "a.first")).unwrap();
        fs::write(dir.path().join("cli/broken.json"), "{").unwrap();
        fs::write(dir.path().join("cli/notes.txt"), "ignored").unwrap();
        let loaded = load_cases(dir.path()).unwrap();
        let keys: Vec<&str> = loaded.iter().map(Loaded::key).collect();
        assert_eq!(keys, ["a.first", "cli/broken.json", "t.echo"]);
        fs::write(dir.path().join("dup.json"), CASE).unwrap();
        let loaded = load_cases(dir.path()).unwrap();
        let dups = loaded
            .iter()
            .filter(|l| matches!(l, Loaded::Invalid { error, .. } if error.contains("duplicate")))
            .count();
        assert_eq!(dups, 2);
        let results = run_all(&cfg("python"), &loaded, Some("a.first"));
        assert_eq!(results.len(), 1);
    }
}
