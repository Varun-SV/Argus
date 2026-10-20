//! Case file model and parser for schema `argus-conformance-case-v1`.
//!
//! The format is documented in `tests/conformance/README.md`. Parsing is strict: unknown keys
//! are rejected so that a typo in a case never silently weakens an assertion.

use std::collections::BTreeMap;
use std::fmt;
use std::path::{Component, Path};

use serde::Deserialize;

/// The only schema identifier this runner accepts.
pub const CASE_SCHEMA: &str = "argus-conformance-case-v1";

/// Implementations a case can declare a status for.
pub const KNOWN_IMPLS: &[&str] = &["python", "rust"];

/// Default per-step timeout when a step does not set `timeout_s`.
pub const DEFAULT_TIMEOUT_S: f64 = 30.0;

/// Upper bound for any step timeout, so a case cannot stall the suite indefinitely.
pub const MAX_TIMEOUT_S: f64 = 600.0;

/// A parsed and validated conformance case.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Case {
    pub schema: String,
    pub id: String,
    pub title: String,
    #[serde(default)]
    pub requirements: Vec<String>,
    pub impls: BTreeMap<String, ImplEntry>,
    /// Operating systems the case applies to (`linux`, `macos`, `windows`); empty means all.
    #[serde(default)]
    pub platforms: Vec<String>,
    #[serde(default)]
    pub setup: Setup,
    /// Extra normalizers, applied after the defaults (`strip_ansi`, `crlf`, `tmpdir`).
    #[serde(default)]
    pub normalizers: Vec<NormalizerSpec>,
    pub steps: Vec<Step>,
    #[serde(default)]
    pub note: Option<String>,
}

/// Per-implementation status of a case.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImplEntry {
    pub status: CaseStatus,
    #[serde(default)]
    pub note: Option<String>,
}

/// How a case is treated for one implementation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "kebab-case")]
pub enum CaseStatus {
    /// Must pass; a failure is a parity bug or regression.
    Active,
    /// Not implemented yet in this implementation: run and reported, never failed.
    Pending,
    /// A known defect in this implementation: the case asserts the correct behaviour, is run
    /// and reported, never failed. The note says what is wrong.
    KnownGap,
    /// Not applicable to this implementation; not run.
    Skip,
}

impl fmt::Display for CaseStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Active => "active",
            Self::Pending => "pending",
            Self::KnownGap => "known-gap",
            Self::Skip => "skip",
        })
    }
}

/// Files and directories materialised into the fresh project directory before step 1.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Setup {
    #[serde(default)]
    pub dirs: Vec<String>,
    #[serde(default)]
    pub files: Vec<SetupFile>,
}

/// One setup file: exactly one of inline `content` or `from` (a path under `tests/golden/`).
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SetupFile {
    pub path: String,
    #[serde(default)]
    pub content: Option<String>,
    #[serde(default)]
    pub from: Option<String>,
}

/// A normalizer: a built-in name, or a custom regex replacement.
#[derive(Debug, Clone, Deserialize)]
#[serde(untagged)]
pub enum NormalizerSpec {
    Named(String),
    Replace { regex: String, replace: String },
}

/// One invocation of the implementation under test.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Step {
    /// Arguments after the executable. Placeholders: `${PROJECT}`, `${SANDBOX}`, `${HOME}`.
    #[serde(default)]
    pub args: Vec<String>,
    /// Environment additions (applied on top of the isolated base environment).
    #[serde(default)]
    pub env: BTreeMap<String, String>,
    /// Bytes written to standard input, then closed. Absent means stdin is empty (null).
    #[serde(default)]
    pub stdin: Option<String>,
    #[serde(default)]
    pub timeout_s: Option<f64>,
    pub expect: Expect,
}

impl Step {
    /// The effective timeout in seconds.
    pub fn timeout(&self) -> f64 {
        self.timeout_s.unwrap_or(DEFAULT_TIMEOUT_S)
    }
}

/// Expected exit status.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(untagged)]
pub enum ExitSpec {
    Code(i32),
    Codes(Vec<i32>),
    /// `"nonzero"` or `"any"`.
    Keyword(String),
}

impl ExitSpec {
    /// Whether `code` satisfies this expectation. `None` (killed by a signal) only satisfies
    /// `nonzero` and `any`.
    pub fn matches(&self, code: Option<i32>) -> bool {
        match self {
            Self::Code(c) => code == Some(*c),
            Self::Codes(cs) => code.is_some_and(|c| cs.contains(&c)),
            Self::Keyword(k) if k == "any" => true,
            Self::Keyword(k) if k == "nonzero" => code != Some(0),
            Self::Keyword(_) => false,
        }
    }
}

impl fmt::Display for ExitSpec {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::Code(c) => write!(f, "{c}"),
            Self::Codes(cs) => {
                let parts: Vec<String> = cs.iter().map(ToString::to_string).collect();
                write!(f, "one of [{}]", parts.join(", "))
            }
            Self::Keyword(k) => f.write_str(k),
        }
    }
}

/// Everything checked after one step.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Expect {
    #[serde(default)]
    pub exit: Option<ExitSpec>,
    /// True when the step is expected to still be running at its timeout (it is then killed
    /// and the output collected so far is checked). Exclusive with `exit`.
    #[serde(default)]
    pub timeout: bool,
    #[serde(default)]
    pub stdout: Option<TextAssert>,
    #[serde(default)]
    pub stderr: Option<TextAssert>,
    /// Standard output followed by standard error. Prefer this for information-content checks,
    /// because implementations may legitimately print a message on either stream.
    #[serde(default)]
    pub output: Option<TextAssert>,
    #[serde(default)]
    pub files: Vec<FileAssert>,
    #[serde(default)]
    pub json: Vec<JsonAssert>,
}

/// Assertions on a normalized text stream.
#[derive(Debug, Clone, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TextAssert {
    #[serde(default)]
    pub contains: Vec<String>,
    #[serde(default)]
    pub not_contains: Vec<String>,
    #[serde(default)]
    pub regex: Vec<String>,
    #[serde(default)]
    pub not_regex: Vec<String>,
    #[serde(default)]
    pub empty: Option<bool>,
}

/// Directory a file assertion is resolved against.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Root {
    /// The temp project directory (the working directory of every step).
    #[default]
    Project,
    /// The whole sandbox: project, home, XDG and temp directories.
    Sandbox,
    Home,
    XdgConfig,
    XdgData,
    XdgState,
    XdgCache,
}

/// Expected kind of a filesystem entry.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EntryKind {
    File,
    Dir,
}

/// Assertions on a path after a step.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct FileAssert {
    pub path: String,
    #[serde(default)]
    pub root: Root,
    #[serde(default)]
    pub exists: Option<bool>,
    #[serde(default)]
    pub kind: Option<EntryKind>,
    /// Substrings the file's content must contain (implies a readable UTF-8 file).
    #[serde(default)]
    pub contains: Vec<String>,
    #[serde(default)]
    pub not_contains: Vec<String>,
    /// Byte strings that must not occur in ANY file below `path` (a file or directory);
    /// used to prove that a secret value never reaches disk in plaintext.
    #[serde(default)]
    pub tree_not_contains: Vec<String>,
}

/// Assertion on a value inside a JSON file produced by a step.
#[derive(Debug, Clone, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct JsonAssert {
    pub file: String,
    #[serde(default)]
    pub root: Root,
    /// JSON path subset: `$`, `.key`, `["key"]`, `[index]`.
    pub path: String,
    #[serde(default)]
    pub exists: Option<bool>,
    #[serde(default)]
    pub equals: Option<serde_json::Value>,
    /// `null`, `bool`, `number`, `string`, `array` or `object`.
    #[serde(default, rename = "type")]
    pub value_type: Option<String>,
    /// For strings: substring; for arrays: an element equal to this string.
    #[serde(default)]
    pub contains: Option<String>,
    /// Minimum length of a string, array or object.
    #[serde(default)]
    pub min_len: Option<usize>,
}

/// A case file that could not be loaded.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CaseError {
    pub message: String,
}

impl fmt::Display for CaseError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

impl std::error::Error for CaseError {}

fn err(message: impl Into<String>) -> CaseError {
    CaseError {
        message: message.into(),
    }
}

/// Parse and validate a case from JSON text.
pub fn parse_case(text: &str) -> Result<Case, CaseError> {
    let case: Case =
        serde_json::from_str(text).map_err(|e| err(format!("invalid case JSON: {e}")))?;
    validate(&case)?;
    Ok(case)
}

/// Whether a relative path is safe to join below a sandbox directory: not empty, not
/// absolute, no `..`, no root or prefix components.
pub fn is_safe_relative(path: &str) -> bool {
    if path.is_empty() || path.contains('\\') {
        return false;
    }
    let p = Path::new(path);
    p.components()
        .all(|c| matches!(c, Component::Normal(_) | Component::CurDir))
}

fn validate_id(id: &str) -> Result<(), CaseError> {
    let ok = !id.is_empty()
        && id.len() <= 120
        && id
            .chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || "._-".contains(c))
        && id.starts_with(|c: char| c.is_ascii_lowercase());
    if ok {
        Ok(())
    } else {
        Err(err(format!(
            "id {id:?} must start with a lowercase letter and use only [a-z0-9._-] (max 120)"
        )))
    }
}

fn validate_text(where_: &str, t: &TextAssert) -> Result<(), CaseError> {
    for r in t.regex.iter().chain(&t.not_regex) {
        regex::Regex::new(r).map_err(|e| err(format!("{where_}: invalid regex {r:?}: {e}")))?;
    }
    Ok(())
}

fn validate(case: &Case) -> Result<(), CaseError> {
    if case.schema != CASE_SCHEMA {
        return Err(err(format!(
            "schema must be {CASE_SCHEMA:?}, found {:?}",
            case.schema
        )));
    }
    validate_id(&case.id)?;
    if case.title.trim().is_empty() {
        return Err(err("title must not be empty"));
    }
    if case.impls.is_empty() {
        return Err(err("impls must name at least one implementation"));
    }
    for name in case.impls.keys() {
        if !KNOWN_IMPLS.contains(&name.as_str()) {
            return Err(err(format!("impls: unknown implementation {name:?}")));
        }
    }
    for (name, entry) in &case.impls {
        let needs_note = matches!(entry.status, CaseStatus::KnownGap | CaseStatus::Pending);
        if needs_note && entry.note.as_deref().is_none_or(|n| n.trim().is_empty()) {
            return Err(err(format!(
                "impls.{name}: status {} requires a note",
                entry.status
            )));
        }
    }
    for p in &case.platforms {
        if !["linux", "macos", "windows"].contains(&p.as_str()) {
            return Err(err(format!("platforms: unknown platform {p:?}")));
        }
    }
    for d in &case.setup.dirs {
        if !is_safe_relative(d) {
            return Err(err(format!("setup.dirs: unsafe path {d:?}")));
        }
    }
    for f in &case.setup.files {
        if !is_safe_relative(&f.path) {
            return Err(err(format!("setup.files: unsafe path {:?}", f.path)));
        }
        match (&f.content, &f.from) {
            (Some(_), None) => {}
            (None, Some(from)) => {
                if !is_safe_relative(from) || !from.starts_with("tests/golden/") {
                    return Err(err(format!(
                        "setup.files {:?}: `from` must be a relative path under tests/golden/",
                        f.path
                    )));
                }
            }
            _ => {
                return Err(err(format!(
                    "setup.files {:?}: give exactly one of `content` or `from`",
                    f.path
                )));
            }
        }
    }
    for n in &case.normalizers {
        match n {
            NormalizerSpec::Named(name) => {
                if !crate::normalize::BUILTIN_NAMES.contains(&name.as_str()) {
                    return Err(err(format!("normalizers: unknown normalizer {name:?}")));
                }
            }
            NormalizerSpec::Replace { regex, .. } => {
                regex::Regex::new(regex)
                    .map_err(|e| err(format!("normalizers: invalid regex {regex:?}: {e}")))?;
            }
        }
    }
    if case.steps.is_empty() {
        return Err(err("steps must contain at least one step"));
    }
    for (i, step) in case.steps.iter().enumerate() {
        let at = format!("steps[{i}]");
        let t = step.timeout();
        if !(t > 0.0 && t <= MAX_TIMEOUT_S) {
            return Err(err(format!(
                "{at}: timeout_s must be in (0, {MAX_TIMEOUT_S}]"
            )));
        }
        let e = &step.expect;
        match (&e.exit, e.timeout) {
            (None, false) => {
                return Err(err(format!("{at}: expect needs `exit` or `timeout: true`")));
            }
            (Some(_), true) => {
                return Err(err(format!(
                    "{at}: `exit` and `timeout: true` are exclusive"
                )));
            }
            (Some(ExitSpec::Keyword(k)), _) if k != "any" && k != "nonzero" => {
                return Err(err(format!(
                    "{at}: exit keyword must be \"nonzero\" or \"any\""
                )));
            }
            (Some(ExitSpec::Codes(cs)), _) if cs.is_empty() => {
                return Err(err(format!("{at}: exit code list must not be empty")));
            }
            _ => {}
        }
        for (label, t) in [
            ("stdout", &e.stdout),
            ("stderr", &e.stderr),
            ("output", &e.output),
        ] {
            if let Some(t) = t {
                validate_text(&format!("{at}.expect.{label}"), t)?;
            }
        }
        for f in &e.files {
            if !(is_safe_relative(&f.path) || f.path == ".") {
                return Err(err(format!("{at}.expect.files: unsafe path {:?}", f.path)));
            }
            let content_checks = !f.contains.is_empty() || !f.not_contains.is_empty();
            if f.exists == Some(false) && (content_checks || f.kind.is_some()) {
                return Err(err(format!(
                    "{at}.expect.files {:?}: content or kind checks contradict exists: false",
                    f.path
                )));
            }
        }
        for j in &e.json {
            if !is_safe_relative(&j.file) {
                return Err(err(format!("{at}.expect.json: unsafe path {:?}", j.file)));
            }
            crate::jsonpath::parse(&j.path)
                .map_err(|m| err(format!("{at}.expect.json {:?}: {m}", j.path)))?;
            if let Some(ty) = &j.value_type {
                if !["null", "bool", "number", "string", "array", "object"].contains(&ty.as_str()) {
                    return Err(err(format!("{at}.expect.json: unknown type {ty:?}")));
                }
            }
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn minimal(extra_step: &str) -> String {
        format!(
            r#"{{"schema":"argus-conformance-case-v1","id":"cli.x","title":"t",
                "impls":{{"python":{{"status":"active"}}}},
                "steps":[{extra_step}]}}"#
        )
    }

    #[test]
    fn parses_minimal_case_with_defaults() {
        let c = parse_case(&minimal(r#"{"args":["--help"],"expect":{"exit":0}}"#)).unwrap();
        assert_eq!(c.id, "cli.x");
        assert_eq!(c.steps.len(), 1);
        assert_eq!(c.steps[0].timeout(), DEFAULT_TIMEOUT_S);
        assert_eq!(c.steps[0].expect.exit, Some(ExitSpec::Code(0)));
        assert!(c.setup.files.is_empty());
    }

    #[test]
    fn rejects_unknown_keys_anywhere() {
        let e = parse_case(&minimal(r#"{"args":[],"expect":{"exit":0,"stdot":{}}}"#)).unwrap_err();
        assert!(e.message.contains("unknown field"), "{e}");
        let top =
            minimal(r#"{"args":[],"expect":{"exit":0}}"#).replacen("\"title\"", "\"titel\"", 1);
        assert!(parse_case(&top).is_err());
    }

    #[test]
    fn rejects_wrong_schema_and_bad_id() {
        let wrong = minimal(r#"{"expect":{"exit":0}}"#).replace("case-v1", "case-v2");
        assert!(parse_case(&wrong).unwrap_err().message.contains("schema"));
        let bad_id = minimal(r#"{"expect":{"exit":0}}"#).replace("cli.x", "CLI X");
        assert!(parse_case(&bad_id).unwrap_err().message.contains("id"));
    }

    #[test]
    fn exit_expectation_forms() {
        let c = parse_case(&minimal(r#"{"expect":{"exit":[0,2]}}"#)).unwrap();
        assert!(c.steps[0].expect.exit.as_ref().unwrap().matches(Some(2)));
        assert!(!c.steps[0].expect.exit.as_ref().unwrap().matches(Some(1)));
        let n = ExitSpec::Keyword("nonzero".into());
        assert!(n.matches(Some(1)) && n.matches(None) && !n.matches(Some(0)));
        assert!(ExitSpec::Keyword("any".into()).matches(None));
        assert!(!ExitSpec::Code(0).matches(None));
        assert!(parse_case(&minimal(r#"{"expect":{"exit":"sometimes"}}"#)).is_err());
        assert!(parse_case(&minimal(r#"{"expect":{"exit":[]}}"#)).is_err());
        assert!(parse_case(&minimal(r#"{"expect":{}}"#)).is_err());
        assert!(parse_case(&minimal(r#"{"expect":{"exit":0,"timeout":true}}"#)).is_err());
        assert!(parse_case(&minimal(r#"{"expect":{"timeout":true}}"#)).is_ok());
    }

    #[test]
    fn rejects_unsafe_paths() {
        for p in ["../x", "/etc/passwd", "", "a/../../b", "a\\b"] {
            assert!(!is_safe_relative(p), "{p}");
        }
        assert!(is_safe_relative(".argus/config.yaml"));
        let bad = r#"{"schema":"argus-conformance-case-v1","id":"a","title":"t",
            "impls":{"python":{"status":"active"}},
            "setup":{"files":[{"path":"../escape","content":"x"}]},
            "steps":[{"expect":{"exit":0}}]}"#;
        assert!(parse_case(bad).unwrap_err().message.contains("unsafe"));
        let golden_outside = bad.replace(
            "../escape\",\"content\":\"x\"",
            "ok\",\"from\":\"argus/cli.py\"",
        );
        assert!(
            parse_case(&golden_outside)
                .unwrap_err()
                .message
                .contains("tests/golden")
        );
        let both = bad.replace(
            "\"../escape\",\"content\":\"x\"",
            "\"ok\",\"content\":\"x\",\"from\":\"tests/golden/a\"",
        );
        assert!(
            parse_case(&both)
                .unwrap_err()
                .message
                .contains("exactly one")
        );
    }

    #[test]
    fn pending_and_known_gap_need_a_note() {
        let base = minimal(r#"{"expect":{"exit":0}}"#);
        let pending = base.replace(r#""status":"active""#, r#""status":"pending""#);
        assert!(parse_case(&pending).unwrap_err().message.contains("note"));
        let noted = base.replace(
            r#""status":"active""#,
            r#""status":"known-gap","note":"defect X""#,
        );
        let c = parse_case(&noted).unwrap();
        assert_eq!(c.impls["python"].status, CaseStatus::KnownGap);
    }

    #[test]
    fn rejects_unknown_impl_platform_normalizer_and_bad_regex() {
        let base = minimal(r#"{"expect":{"exit":0}}"#);
        assert!(parse_case(&base.replace("\"python\"", "\"java\"")).is_err());
        let plat = base.replace("\"steps\"", "\"platforms\":[\"beos\"],\"steps\"");
        assert!(parse_case(&plat).is_err());
        let norm = base.replace("\"steps\"", "\"normalizers\":[\"rot13\"],\"steps\"");
        assert!(parse_case(&norm).is_err());
        let custom = base.replace(
            "\"steps\"",
            "\"normalizers\":[{\"regex\":\"\\\\d+\",\"replace\":\"N\"}],\"steps\"",
        );
        assert!(parse_case(&custom).is_ok());
        let re = minimal(r#"{"expect":{"exit":0,"output":{"regex":["("]}}}"#);
        assert!(
            parse_case(&re)
                .unwrap_err()
                .message
                .contains("invalid regex")
        );
        let jp = minimal(r#"{"expect":{"exit":0,"json":[{"file":"a.json","path":"x"}]}}"#);
        assert!(parse_case(&jp).is_err());
    }

    #[test]
    fn timeout_bounds() {
        assert!(parse_case(&minimal(r#"{"timeout_s":0,"expect":{"exit":0}}"#)).is_err());
        assert!(parse_case(&minimal(r#"{"timeout_s":601,"expect":{"exit":0}}"#)).is_err());
        assert!(parse_case(&minimal(r#"{"timeout_s":0.5,"expect":{"exit":0}}"#)).is_ok());
    }
}
