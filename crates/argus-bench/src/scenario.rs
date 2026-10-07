//! Scenario files (`bench/scenarios/*.toml`), documented in bench/README.md.
//!
//! A scenario names an argv array to measure. There is no shell: every command is an argv
//! array, and the only substitution is the fixed set of `{PLACEHOLDER}` tokens in
//! [`PLACEHOLDERS`]. Unknown fields and unknown placeholders are errors, so a typo cannot
//! silently change what is measured.

use std::collections::BTreeMap;
use std::path::{Component, Path, PathBuf};
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// Schema identifier a scenario file may declare in its `schema` field.
pub const SCENARIO_SCHEMA: &str = "argus-bench-scenario-v1";

/// Placeholders that scenario strings may use, with their meaning (printed by `--help` docs).
pub const PLACEHOLDERS: &[(&str, &str)] = &[
    (
        "PYTHON",
        "Python interpreter: --python, else ARGUS_BENCH_PYTHON, else python3 (python on Windows)",
    ),
    (
        "ARGUS",
        "`argus` console script: --argus, else ARGUS_BENCH_ARGUS, else next to {PYTHON}, else argus",
    ),
    (
        "ARGUS_GUI",
        "`argus-gui` console script: --argus-gui, else ARGUS_BENCH_ARGUS_GUI, else next to {PYTHON}",
    ),
    (
        "ARGUS_NEXT",
        "Rust preview binary: --argus-next, else ARGUS_BENCH_ARGUS_NEXT, else argus-next",
    ),
    (
        "REPO",
        "repository root: --root, else the nearest ancestor of the scenario file with a bench/ dir",
    ),
    ("SCENARIO_DIR", "directory that contains the scenario file"),
    ("WORKDIR", "fresh empty directory created for each run and deleted afterwards"),
    ("OS", "linux, macos or windows"),
    ("OS_FAMILY", "unix or windows"),
    ("EXE_SUFFIX", ".exe on Windows, empty elsewhere"),
    (
        "PLAYWRIGHT_BROWSERS_PATH",
        "the host's Playwright browser cache (env var if set, else the per-OS default)",
    ),
];

/// Errors found while loading or validating a scenario.
#[derive(Debug, thiserror::Error)]
pub enum ScenarioError {
    /// The file could not be read.
    #[error("cannot read scenario {path}: {source}")]
    Read {
        /// File that failed.
        path: String,
        /// Underlying error.
        source: std::io::Error,
    },
    /// The TOML did not parse or did not match the schema.
    #[error("invalid scenario TOML: {0}")]
    Toml(#[from] toml::de::Error),
    /// A field has an invalid value.
    #[error("invalid scenario field `{field}`: {message}")]
    Field {
        /// Field name.
        field: String,
        /// What is wrong.
        message: String,
    },
}

fn field_err(field: &str, message: impl Into<String>) -> ScenarioError {
    ScenarioError::Field {
        field: field.to_owned(),
        message: message.into(),
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawScenario {
    schema: Option<String>,
    name: String,
    description: String,
    #[serde(default)]
    tags: Vec<String>,
    #[serde(default)]
    command: Vec<String>,
    cwd: Option<String>,
    #[serde(default)]
    env: BTreeMap<String, String>,
    #[serde(default)]
    setup: Vec<Vec<String>>,
    #[serde(default)]
    teardown: Vec<Vec<String>>,
    timeout: Option<String>,
    warmup: Option<u32>,
    expected_exit: Option<i32>,
    duration: Option<String>,
    measure_after: Option<String>,
    pending: Option<String>,
    workdir: Option<RawWorkdir>,
    skip_unless: Option<RawProbe>,
    target: Option<Target>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawWorkdir {
    copy_from: Option<String>,
    #[serde(default)]
    create_dirs: Vec<String>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct RawProbe {
    command: Vec<String>,
    reason: String,
    timeout: Option<String>,
}

/// How the per-run working directory `{WORKDIR}` is prepared (outside the measured window).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct WorkdirSpec {
    /// Repository-relative directory copied into `{WORKDIR}` before each run.
    pub copy_from: Option<String>,
    /// `{WORKDIR}`-relative directories created before each run.
    pub create_dirs: Vec<String>,
}

/// A probe that must succeed for the scenario to run; otherwise the result is "skipped".
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Probe {
    /// argv of the probe.
    pub command: Vec<String>,
    /// Reason recorded when the probe fails (no probe output is recorded).
    pub reason: String,
    /// Probe timeout.
    pub timeout: Duration,
}

/// Spec §11 target for this scenario, compared in the report. Absent values are not checked.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Target {
    /// Upper bound on the median wall time, in milliseconds.
    pub wall_ms: Option<f64>,
    /// Upper bound on the median peak tree memory, in MiB (1 MiB = 1,048,576 bytes).
    pub peak_tree_mib: Option<f64>,
    /// Free-text source of the target, e.g. "spec §11 row 2".
    pub source: Option<String>,
}

/// A validated scenario.
#[derive(Debug, Clone, PartialEq)]
pub struct Scenario {
    /// Short identifier (lowercase letters, digits and hyphens).
    pub name: String,
    /// One-paragraph description.
    pub description: String,
    /// Free-form tags.
    pub tags: Vec<String>,
    /// argv measured in each run (templates; placeholders not yet expanded).
    pub command: Vec<String>,
    /// Working directory template; relative paths are resolved against `{REPO}`.
    pub cwd: Option<String>,
    /// Extra environment variables (templates). A value that expands to "" is not set.
    pub env: BTreeMap<String, String>,
    /// argv arrays run before each run, outside the measured window.
    pub setup: Vec<Vec<String>>,
    /// argv arrays run after each run, outside the measured window.
    pub teardown: Vec<Vec<String>>,
    /// Hard limit per run; the whole tree is terminated when it is reached.
    pub timeout: Duration,
    /// Uncounted runs before the measured ones.
    pub warmup: u32,
    /// Expected exit code of the measured command (not checked when `duration` is set).
    pub expected_exit: i32,
    /// When set, each run is stopped after this long (for apps that do not exit by themselves).
    pub duration: Option<Duration>,
    /// Samples before this offset do not count towards the peak (excludes start-up).
    pub measure_after: Duration,
    /// When set, the scenario is a placeholder and is not run.
    pub pending: Option<String>,
    /// Per-run working directory preparation.
    pub workdir: WorkdirSpec,
    /// Optional probe deciding whether the scenario can run on this machine.
    pub skip_unless: Option<Probe>,
    /// Optional target from spec §11.
    pub target: Option<Target>,
}

const DEFAULT_TIMEOUT: Duration = Duration::from_secs(120);
const DEFAULT_PROBE_TIMEOUT: Duration = Duration::from_secs(60);

fn parse_duration(field: &str, value: &str) -> Result<Duration, ScenarioError> {
    let parsed = humantime::parse_duration(value)
        .map_err(|e| field_err(field, format!("`{value}` is not a duration: {e}")))?;
    if parsed.is_zero() {
        return Err(field_err(field, "must be greater than zero"));
    }
    Ok(parsed)
}

/// Returns the placeholder names used in `template`, in order of appearance.
///
/// A placeholder is `{` + an uppercase identifier (`A-Z`, `0-9`, `_`, starting with a letter)
/// + `}`. Other braces, such as Python dict literals in `-c` code, are left alone.
pub fn placeholders_in(template: &str) -> Vec<&str> {
    let mut found = Vec::new();
    let bytes = template.as_bytes();
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'{' {
            if let Some(len) = placeholder_len(&bytes[i + 1..]) {
                found.push(&template[i + 1..i + 1 + len]);
                i += len + 2;
                continue;
            }
        }
        i += 1;
    }
    found
}

/// Length of a placeholder name at the start of `rest` when it is followed by `}`.
fn placeholder_len(rest: &[u8]) -> Option<usize> {
    let first = *rest.first()?;
    if !first.is_ascii_uppercase() {
        return None;
    }
    let len = rest
        .iter()
        .take_while(|b| b.is_ascii_uppercase() || b.is_ascii_digit() || **b == b'_')
        .count();
    (rest.get(len) == Some(&b'}')).then_some(len)
}

/// Expands placeholders in `template` using `values`. Unknown placeholders are an error.
pub fn expand(template: &str, values: &BTreeMap<String, String>) -> Result<String, ScenarioError> {
    let mut out = String::with_capacity(template.len());
    let bytes = template.as_bytes();
    let mut i = 0;
    let mut copied = 0;
    while i < bytes.len() {
        if bytes[i] == b'{' {
            if let Some(len) = placeholder_len(&bytes[i + 1..]) {
                let name = &template[i + 1..i + 1 + len];
                let value = values.get(name).ok_or_else(|| {
                    field_err("placeholder", format!("unknown or unset placeholder {{{name}}}"))
                })?;
                out.push_str(&template[copied..i]);
                out.push_str(value);
                i += len + 2;
                copied = i;
                continue;
            }
        }
        i += 1;
    }
    out.push_str(&template[copied..]);
    Ok(out)
}

fn check_placeholders(field: &str, template: &str) -> Result<(), ScenarioError> {
    for name in placeholders_in(template) {
        if !PLACEHOLDERS.iter().any(|(known, _)| *known == name) {
            return Err(field_err(field, format!("unknown placeholder {{{name}}}")));
        }
    }
    Ok(())
}

fn check_argv(field: &str, argv: &[String]) -> Result<(), ScenarioError> {
    if argv.is_empty() || argv[0].is_empty() {
        return Err(field_err(field, "must be a non-empty argv array"));
    }
    for arg in argv {
        if arg.contains('\0') {
            return Err(field_err(field, "arguments must not contain NUL"));
        }
        check_placeholders(field, arg)?;
    }
    Ok(())
}

/// Checks a relative path that must stay inside its base directory.
fn check_relative(field: &str, value: &str) -> Result<(), ScenarioError> {
    let path = Path::new(value);
    if value.is_empty() || value.contains('\\') {
        return Err(field_err(field, "must be a non-empty path using / separators"));
    }
    for component in path.components() {
        match component {
            Component::Normal(_) | Component::CurDir => {}
            _ => {
                return Err(field_err(
                    field,
                    format!("`{value}` must be relative and must not contain `..`"),
                ));
            }
        }
    }
    Ok(())
}

impl Scenario {
    /// Parses and validates a scenario from TOML text.
    pub fn from_toml_str(text: &str) -> Result<Self, ScenarioError> {
        let raw: RawScenario = toml::from_str(text)?;
        Self::validate(raw)
    }

    /// Reads, parses and validates a scenario file.
    pub fn load(path: &Path) -> Result<Self, ScenarioError> {
        let text = std::fs::read_to_string(path).map_err(|source| ScenarioError::Read {
            path: path.display().to_string(),
            source,
        })?;
        Self::from_toml_str(&text)
    }

    fn validate(raw: RawScenario) -> Result<Self, ScenarioError> {
        if let Some(schema) = &raw.schema {
            if schema != SCENARIO_SCHEMA {
                return Err(field_err("schema", format!("expected `{SCENARIO_SCHEMA}`")));
            }
        }
        let name_ok = !raw.name.is_empty()
            && raw.name.len() <= 64
            && raw
                .name
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'-');
        if !name_ok {
            return Err(field_err(
                "name",
                "use 1-64 lowercase letters, digits and hyphens",
            ));
        }
        if raw.description.trim().is_empty() {
            return Err(field_err("description", "must not be empty"));
        }
        let pending = match raw.pending {
            Some(p) if p.trim().is_empty() => {
                return Err(field_err("pending", "must say what is missing"));
            }
            other => other,
        };
        check_argv("command", &raw.command)?;
        if let Some(cwd) = &raw.cwd {
            check_placeholders("cwd", cwd)?;
        }
        for (key, value) in &raw.env {
            if key.is_empty() || key.contains('=') || key.contains('\0') || value.contains('\0') {
                return Err(field_err("env", format!("invalid variable `{key}`")));
            }
            check_placeholders("env", value)?;
        }
        for argv in &raw.setup {
            check_argv("setup", argv)?;
        }
        for argv in &raw.teardown {
            check_argv("teardown", argv)?;
        }
        let duration = raw
            .duration
            .as_deref()
            .map(|d| parse_duration("duration", d))
            .transpose()?;
        let measure_after = match raw.measure_after.as_deref() {
            Some(value) => parse_duration("measure_after", value)?,
            None => Duration::ZERO,
        };
        if let Some(duration) = duration {
            if measure_after >= duration {
                return Err(field_err("measure_after", "must be shorter than `duration`"));
            }
        }
        let timeout = match raw.timeout.as_deref() {
            Some(value) => parse_duration("timeout", value)?,
            None => duration.map_or(DEFAULT_TIMEOUT, |d| d + Duration::from_secs(30)),
        };
        if duration.is_some_and(|d| timeout <= d) {
            return Err(field_err("timeout", "must be longer than `duration`"));
        }
        let workdir = match raw.workdir {
            Some(w) => {
                if let Some(from) = &w.copy_from {
                    check_relative("workdir.copy_from", from)?;
                }
                for dir in &w.create_dirs {
                    check_relative("workdir.create_dirs", dir)?;
                }
                WorkdirSpec {
                    copy_from: w.copy_from,
                    create_dirs: w.create_dirs,
                }
            }
            None => WorkdirSpec::default(),
        };
        let skip_unless = match raw.skip_unless {
            Some(probe) => {
                check_argv("skip_unless.command", &probe.command)?;
                if probe.reason.trim().is_empty() {
                    return Err(field_err("skip_unless.reason", "must not be empty"));
                }
                Some(Probe {
                    command: probe.command,
                    reason: probe.reason,
                    timeout: probe
                        .timeout
                        .as_deref()
                        .map(|t| parse_duration("skip_unless.timeout", t))
                        .transpose()?
                        .unwrap_or(DEFAULT_PROBE_TIMEOUT),
                })
            }
            None => None,
        };
        if let Some(target) = &raw.target {
            let positive = |v: Option<f64>| v.is_none_or(|x| x.is_finite() && x > 0.0);
            if !positive(target.wall_ms) || !positive(target.peak_tree_mib) {
                return Err(field_err("target", "bounds must be positive numbers"));
            }
        }
        Ok(Self {
            name: raw.name,
            description: raw.description.trim().to_owned(),
            tags: raw.tags,
            command: raw.command,
            cwd: raw.cwd,
            env: raw.env,
            setup: raw.setup,
            teardown: raw.teardown,
            timeout,
            warmup: raw.warmup.unwrap_or(1),
            expected_exit: raw.expected_exit.unwrap_or(0),
            duration,
            measure_after,
            pending,
            workdir,
            skip_unless,
            target: raw.target,
        })
    }
}

/// Finds the repository root for a scenario file: the nearest ancestor with a `bench/` dir.
pub fn find_repo_root(scenario_file: &Path) -> Option<PathBuf> {
    let absolute = std::path::absolute(scenario_file).ok()?;
    absolute
        .ancestors()
        .skip(1)
        .find(|dir| dir.join("bench").is_dir())
        .map(Path::to_path_buf)
}

#[cfg(test)]
mod tests {
    use super::*;

    const MINIMAL: &str = r#"
name = "demo"
description = "A demo."
command = ["{PYTHON}", "-c", "pass"]
"#;

    #[test]
    fn minimal_scenario_gets_defaults() {
        let s = Scenario::from_toml_str(MINIMAL).unwrap();
        assert_eq!(s.warmup, 1);
        assert_eq!(s.expected_exit, 0);
        assert_eq!(s.timeout, DEFAULT_TIMEOUT);
        assert_eq!(s.measure_after, Duration::ZERO);
        assert!(s.pending.is_none());
    }

    #[test]
    fn unknown_fields_are_rejected() {
        let text = format!("{MINIMAL}\nshell = true\n");
        assert!(matches!(
            Scenario::from_toml_str(&text),
            Err(ScenarioError::Toml(_))
        ));
    }

    #[test]
    fn unknown_placeholder_is_rejected() {
        let text = r#"
name = "demo"
description = "d"
command = ["{PYTON}", "-c", "pass"]
"#;
        let err = Scenario::from_toml_str(text).unwrap_err().to_string();
        assert!(err.contains("{PYTON}"), "{err}");
    }

    #[test]
    fn python_braces_are_not_placeholders() {
        assert_eq!(placeholders_in("print({'a': 1}) {x} {X1} {WORKDIR}/h"), vec!["X1", "WORKDIR"]);
        let mut values = BTreeMap::new();
        values.insert("WORKDIR".to_owned(), "/w".to_owned());
        assert_eq!(expand("{'k': {WORKDIR}}/{", &values).unwrap(), "{'k': /w}/{");
        assert!(expand("{NOPE}", &values).is_err());
    }

    #[test]
    fn workdir_paths_must_stay_inside() {
        for bad in ["../x", "/abs", "a/../../b", "a\\b", ""] {
            let text = format!(
                "{MINIMAL}\n[workdir]\ncopy_from = {bad:?}\n"
            );
            assert!(Scenario::from_toml_str(&text).is_err(), "{bad}");
        }
        let ok = format!("{MINIMAL}\n[workdir]\ncopy_from = \"bench/fixtures/x\"\n");
        assert!(Scenario::from_toml_str(&ok).is_ok());
    }

    #[test]
    fn duration_rules() {
        let text = format!("{MINIMAL}\nduration = \"30s\"\nmeasure_after = \"10s\"\n");
        let s = Scenario::from_toml_str(&text).unwrap();
        assert_eq!(s.duration, Some(Duration::from_secs(30)));
        assert_eq!(s.timeout, Duration::from_secs(60));
        let bad = format!("{MINIMAL}\nduration = \"5s\"\nmeasure_after = \"10s\"\n");
        assert!(Scenario::from_toml_str(&bad).is_err());
        let bad = format!("{MINIMAL}\nduration = \"5s\"\ntimeout = \"5s\"\n");
        assert!(Scenario::from_toml_str(&bad).is_err());
    }

    #[test]
    fn pending_must_explain() {
        let text = format!("{MINIMAL}\npending = \" \"\n");
        assert!(Scenario::from_toml_str(&text).is_err());
        let text = format!("{MINIMAL}\npending = \"needs argus-next feature: run\"\n");
        assert!(Scenario::from_toml_str(&text).unwrap().pending.is_some());
    }

    #[test]
    fn bad_names_and_schema() {
        let text = MINIMAL.replace("\"demo\"", "\"Demo Name\"");
        assert!(Scenario::from_toml_str(&text).is_err());
        let text = format!("schema = \"other\"\n{MINIMAL}");
        assert!(Scenario::from_toml_str(&text).is_err());
        let text = format!("schema = \"{SCENARIO_SCHEMA}\"\n{MINIMAL}");
        assert!(Scenario::from_toml_str(&text).is_ok());
    }
}
