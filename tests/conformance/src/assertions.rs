//! The assertion engine: checks one step's [`Expect`] against its normalized output and the
//! sandbox filesystem. Every check that fails adds one human-readable message; an empty list
//! means the step passed.

use std::fs;
use std::path::Path;

use regex::Regex;

use crate::case::{EntryKind, Expect, FileAssert, JsonAssert, TextAssert};
use crate::jsonpath;
use crate::sandbox::Sandbox;

/// Normalized observations of one step.
#[derive(Debug, Clone, Default)]
pub struct Observed {
    pub exit_code: Option<i32>,
    pub timed_out: bool,
    pub stdout: String,
    pub stderr: String,
}

impl Observed {
    /// Standard output followed by standard error.
    pub fn output(&self) -> String {
        if self.stderr.is_empty() {
            self.stdout.clone()
        } else if self.stdout.is_empty() || self.stdout.ends_with('\n') {
            format!("{}{}", self.stdout, self.stderr)
        } else {
            format!("{}\n{}", self.stdout, self.stderr)
        }
    }
}

fn excerpt(s: &str) -> String {
    const MAX: usize = 300;
    let one_line = s.replace('\n', "\\n");
    if one_line.chars().count() <= MAX {
        one_line
    } else {
        let head: String = one_line.chars().take(MAX).collect();
        format!("{head}...")
    }
}

/// Check text assertions on one stream.
pub fn check_text(label: &str, text: &str, t: &TextAssert, failures: &mut Vec<String>) {
    for needle in &t.contains {
        if !text.contains(needle.as_str()) {
            failures.push(format!(
                "{label} does not contain {needle:?}; got: {}",
                excerpt(text)
            ));
        }
    }
    for needle in &t.not_contains {
        // Never echo the text around a forbidden value: it may be a secret.
        if text.contains(needle.as_str()) {
            failures.push(format!(
                "{label} contains forbidden text ({} bytes)",
                needle.len()
            ));
        }
    }
    for pattern in &t.regex {
        match Regex::new(pattern) {
            Ok(r) if r.is_match(text) => {}
            Ok(_) => failures.push(format!(
                "{label} does not match /{pattern}/; got: {}",
                excerpt(text)
            )),
            Err(e) => failures.push(format!("{label}: invalid regex /{pattern}/: {e}")),
        }
    }
    for pattern in &t.not_regex {
        match Regex::new(pattern) {
            Ok(r) => {
                if let Some(m) = r.find(text) {
                    failures.push(format!(
                        "{label} matches forbidden /{pattern}/ at {:?}",
                        excerpt(m.as_str())
                    ));
                }
            }
            Err(e) => failures.push(format!("{label}: invalid regex /{pattern}/: {e}")),
        }
    }
    if let Some(want_empty) = t.empty {
        if want_empty != text.trim().is_empty() {
            let what = if want_empty { "empty" } else { "non-empty" };
            failures.push(format!("{label} expected {what}; got: {}", excerpt(text)));
        }
    }
}

fn tree_contains(path: &Path, needle: &[u8], depth: usize) -> std::io::Result<Option<String>> {
    let meta = fs::symlink_metadata(path)?;
    if meta.is_file() {
        let bytes = fs::read(path)?;
        if !needle.is_empty() && bytes.windows(needle.len()).any(|w| w == needle) {
            return Ok(Some(path.display().to_string()));
        }
    } else if meta.is_dir() && depth < 64 {
        let mut entries: Vec<_> = fs::read_dir(path)?.filter_map(Result::ok).collect();
        entries.sort_by_key(std::fs::DirEntry::file_name);
        for e in entries {
            if let Some(hit) = tree_contains(&e.path(), needle, depth + 1)? {
                return Ok(Some(hit));
            }
        }
    }
    Ok(None)
}

/// Check one file assertion.
pub fn check_file(sb: &Sandbox, f: &FileAssert, failures: &mut Vec<String>) {
    let base = sb.resolve_root(f.root);
    let path = if f.path == "." {
        base.clone()
    } else {
        base.join(&f.path)
    };
    let label = format!("file {:?} (root {:?})", f.path, f.root);
    let meta = fs::metadata(&path).ok();
    match (f.exists, &meta) {
        (Some(true), None) => {
            failures.push(format!("{label} does not exist"));
            return;
        }
        (Some(false), Some(_)) => {
            failures.push(format!("{label} exists but must not"));
            return;
        }
        _ => {}
    }
    if let (Some(kind), Some(m)) = (f.kind, &meta) {
        let actual = if m.is_dir() {
            EntryKind::Dir
        } else {
            EntryKind::File
        };
        if actual != kind {
            failures.push(format!("{label} is a {actual:?}, expected {kind:?}"));
        }
    } else if f.kind.is_some() && meta.is_none() {
        failures.push(format!("{label} does not exist"));
    }
    if !f.contains.is_empty() || !f.not_contains.is_empty() {
        match fs::read(&path) {
            Ok(bytes) => {
                let text = String::from_utf8_lossy(&bytes);
                let t = TextAssert {
                    contains: f.contains.clone(),
                    not_contains: f.not_contains.clone(),
                    ..TextAssert::default()
                };
                check_text(&label, &text, &t, failures);
            }
            Err(e) => failures.push(format!("{label} cannot be read: {e}")),
        }
    }
    for needle in &f.tree_not_contains {
        if meta.is_none() {
            // A missing path would pass for any needle, so it is a failure.
            failures.push(format!(
                "{label} does not exist, so it cannot be shown to omit a value"
            ));
            continue;
        }
        match tree_contains(&path, needle.as_bytes(), 0) {
            Ok(None) => {}
            Ok(Some(hit)) => {
                let shown = hit.replace(&sb.root().display().to_string(), "<TMP>");
                failures.push(format!("{label}: forbidden bytes found in {shown}"));
            }
            Err(e) => failures.push(format!("{label}: cannot scan: {e}")),
        }
    }
}

/// Check one JSON assertion.
pub fn check_json(sb: &Sandbox, j: &JsonAssert, failures: &mut Vec<String>) {
    let label = format!("json {:?} at {}", j.file, j.path);
    let file = sb.resolve_root(j.root).join(&j.file);
    let text = match fs::read_to_string(&file) {
        Ok(t) => t,
        Err(e) => {
            failures.push(format!("{label}: cannot read file: {e}"));
            return;
        }
    };
    let doc: serde_json::Value = match serde_json::from_str(&text) {
        Ok(v) => v,
        Err(e) => {
            failures.push(format!("{label}: not valid JSON: {e}"));
            return;
        }
    };
    let path = match jsonpath::parse(&j.path) {
        Ok(p) => p,
        Err(e) => {
            failures.push(format!("{label}: {e}"));
            return;
        }
    };
    let found = jsonpath::resolve(&doc, &path);
    if let Some(want) = j.exists {
        if want != found.is_some() {
            let what = if want {
                "missing"
            } else {
                "present but must not be"
            };
            failures.push(format!("{label}: value {what}"));
            return;
        }
    }
    let needs_value =
        j.equals.is_some() || j.value_type.is_some() || j.contains.is_some() || j.min_len.is_some();
    let Some(v) = found else {
        if needs_value {
            failures.push(format!("{label}: value missing"));
        }
        return;
    };
    if let Some(want) = &j.equals {
        if v != want {
            failures.push(format!(
                "{label}: expected {want}, got {}",
                excerpt(&v.to_string())
            ));
        }
    }
    if let Some(ty) = &j.value_type {
        let actual = jsonpath::type_name(v);
        if actual != ty {
            failures.push(format!("{label}: expected type {ty}, got {actual}"));
        }
    }
    if let Some(needle) = &j.contains {
        let ok = match v {
            serde_json::Value::String(s) => s.contains(needle.as_str()),
            serde_json::Value::Array(a) => a.iter().any(|e| e.as_str() == Some(needle.as_str())),
            _ => false,
        };
        if !ok {
            failures.push(format!("{label}: does not contain {needle:?}"));
        }
    }
    if let Some(min) = j.min_len {
        let len = match v {
            serde_json::Value::String(s) => Some(s.chars().count()),
            serde_json::Value::Array(a) => Some(a.len()),
            serde_json::Value::Object(o) => Some(o.len()),
            _ => None,
        };
        match len {
            Some(n) if n >= min => {}
            Some(n) => failures.push(format!("{label}: length {n} < {min}")),
            None => failures.push(format!("{label}: value has no length")),
        }
    }
}

/// Check everything a step expects. Returns the failure messages (empty = pass).
pub fn check_step(sb: &Sandbox, expect: &Expect, obs: &Observed) -> Vec<String> {
    let mut failures = Vec::new();
    if expect.timeout {
        if !obs.timed_out || obs.exit_code.is_some() {
            failures.push(format!(
                "expected the step to still be running at its timeout; it exited with {:?}",
                obs.exit_code
            ));
        }
    } else if obs.timed_out {
        failures.push("timed out (process group killed)".to_owned());
    } else if let Some(exit) = &expect.exit {
        if !exit.matches(obs.exit_code) {
            let got = obs
                .exit_code
                .map_or_else(|| "a signal".to_owned(), |c| c.to_string());
            failures.push(format!("exit code: expected {exit}, got {got}"));
        }
    }
    if let Some(t) = &expect.stdout {
        check_text("stdout", &obs.stdout, t, &mut failures);
    }
    if let Some(t) = &expect.stderr {
        check_text("stderr", &obs.stderr, t, &mut failures);
    }
    if let Some(t) = &expect.output {
        check_text("output", &obs.output(), t, &mut failures);
    }
    for f in &expect.files {
        check_file(sb, f, &mut failures);
    }
    for j in &expect.json {
        check_json(sb, j, &mut failures);
    }
    failures
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::case::{ExitSpec, Root};

    fn text(contains: &[&str], not: &[&str], re: &[&str], not_re: &[&str]) -> TextAssert {
        let v = |s: &[&str]| s.iter().map(|x| (*x).to_owned()).collect();
        TextAssert {
            contains: v(contains),
            not_contains: v(not),
            regex: v(re),
            not_regex: v(not_re),
            empty: None,
        }
    }

    fn obs(code: i32, out: &str, err: &str) -> Observed {
        Observed {
            exit_code: Some(code),
            timed_out: false,
            stdout: out.into(),
            stderr: err.into(),
        }
    }

    #[test]
    fn text_assertions() {
        let mut f = Vec::new();
        check_text(
            "o",
            "Usage: argus run\n  --dry-run",
            &text(
                &["--dry-run"],
                &["secret"],
                &[r"(?m)^Usage:"],
                &[r"Traceback"],
            ),
            &mut f,
        );
        assert!(f.is_empty(), "{f:?}");
        check_text(
            "o",
            "abc",
            &text(&["zzz"], &["b"], &["^x"], &["c$"]),
            &mut f,
        );
        assert_eq!(f.len(), 4, "{f:?}");
    }

    #[test]
    fn forbidden_text_is_never_echoed() {
        let mut f = Vec::new();
        check_text(
            "stdout",
            "token=DUMMY-SECRET-1 end",
            &text(&[], &["DUMMY-SECRET-1"], &[], &[]),
            &mut f,
        );
        assert_eq!(f.len(), 1);
        assert!(!f[0].contains("DUMMY-SECRET-1"), "{}", f[0]);
    }

    #[test]
    fn empty_assertion() {
        let mut f = Vec::new();
        let t = TextAssert {
            empty: Some(true),
            ..TextAssert::default()
        };
        check_text("s", "  \n", &t, &mut f);
        assert!(f.is_empty());
        check_text("s", "x", &t, &mut f);
        assert_eq!(f.len(), 1);
    }

    #[test]
    fn exit_and_timeout_expectations() {
        let sb = Sandbox::create(None).unwrap();
        let e = Expect {
            exit: Some(ExitSpec::Codes(vec![0, 2])),
            ..Expect::default()
        };
        assert!(check_step(&sb, &e, &obs(2, "", "")).is_empty());
        assert_eq!(check_step(&sb, &e, &obs(1, "", "")).len(), 1);
        let timed = Observed {
            exit_code: None,
            timed_out: true,
            ..Observed::default()
        };
        let f = check_step(&sb, &e, &timed);
        assert!(f[0].contains("timed out"), "{f:?}");
        let want_timeout = Expect {
            timeout: true,
            ..Expect::default()
        };
        assert!(check_step(&sb, &want_timeout, &timed).is_empty());
        assert_eq!(check_step(&sb, &want_timeout, &obs(0, "", "")).len(), 1);
    }

    #[test]
    fn output_joins_streams() {
        assert_eq!(obs(0, "a", "b").output(), "a\nb");
        assert_eq!(obs(0, "a\n", "b").output(), "a\nb");
        assert_eq!(obs(0, "", "b").output(), "b");
        let sb = Sandbox::create(None).unwrap();
        let e = Expect {
            exit: Some(ExitSpec::Code(2)),
            output: Some(text(&["No such command", "Usage"], &[], &[], &[])),
            ..Expect::default()
        };
        assert!(check_step(&sb, &e, &obs(2, "Usage: x", "Error: No such command")).is_empty());
    }

    #[test]
    fn file_assertions() {
        let sb = Sandbox::create(None).unwrap();
        fs::create_dir_all(sb.project().join(".argus/runs")).unwrap();
        fs::write(
            sb.project().join(".argus/config.yaml"),
            "provider: ollama\n",
        )
        .unwrap();
        fs::create_dir_all(sb.root().join("xdg/data/argus")).unwrap();
        fs::write(sb.root().join("xdg/data/argus/store"), b"\x00cipher\x01").unwrap();
        let fa = |path: &str| FileAssert {
            path: path.into(),
            root: Root::Project,
            exists: None,
            kind: None,
            contains: vec![],
            not_contains: vec![],
            tree_not_contains: vec![],
        };
        let mut f = Vec::new();
        check_file(
            &sb,
            &FileAssert {
                exists: Some(true),
                kind: Some(EntryKind::Dir),
                ..fa(".argus/runs")
            },
            &mut f,
        );
        check_file(
            &sb,
            &FileAssert {
                contains: vec!["provider:".into()],
                ..fa(".argus/config.yaml")
            },
            &mut f,
        );
        check_file(
            &sb,
            &FileAssert {
                exists: Some(false),
                ..fa(".argus/nope")
            },
            &mut f,
        );
        check_file(
            &sb,
            &FileAssert {
                root: Root::Sandbox,
                tree_not_contains: vec!["PLAINTEXT".into()],
                ..fa(".")
            },
            &mut f,
        );
        assert!(f.is_empty(), "{f:?}");
        check_file(
            &sb,
            &FileAssert {
                exists: Some(true),
                ..fa(".argus/nope")
            },
            &mut f,
        );
        check_file(
            &sb,
            &FileAssert {
                kind: Some(EntryKind::File),
                ..fa(".argus/runs")
            },
            &mut f,
        );
        check_file(
            &sb,
            &FileAssert {
                not_contains: vec!["ollama".into()],
                ..fa(".argus/config.yaml")
            },
            &mut f,
        );
        check_file(
            &sb,
            &FileAssert {
                root: Root::XdgData,
                tree_not_contains: vec!["cipher".into()],
                ..fa(".")
            },
            &mut f,
        );
        assert_eq!(f.len(), 4, "{f:?}");
        assert!(f[3].contains("<TMP>"), "sandbox path hidden: {}", f[3]);
    }

    #[test]
    fn json_assertions() {
        let sb = Sandbox::create(None).unwrap();
        fs::write(
            sb.project().join("r.json"),
            r#"{"status":"pass","steps":[{"n":1},{"n":2}],"tags":["a","b"],"name":"smoke"}"#,
        )
        .unwrap();
        fs::write(sb.project().join("bad.json"), "{nope").unwrap();
        let ja = |path: &str| JsonAssert {
            file: "r.json".into(),
            root: Root::Project,
            path: path.into(),
            exists: None,
            equals: None,
            value_type: None,
            contains: None,
            min_len: None,
        };
        let mut f = Vec::new();
        check_json(
            &sb,
            &JsonAssert {
                equals: Some(serde_json::json!("pass")),
                ..ja("$.status")
            },
            &mut f,
        );
        check_json(
            &sb,
            &JsonAssert {
                value_type: Some("array".into()),
                min_len: Some(2),
                ..ja("$.steps")
            },
            &mut f,
        );
        check_json(
            &sb,
            &JsonAssert {
                equals: Some(serde_json::json!(2)),
                ..ja("$.steps[1].n")
            },
            &mut f,
        );
        check_json(
            &sb,
            &JsonAssert {
                contains: Some("b".into()),
                ..ja("$.tags")
            },
            &mut f,
        );
        check_json(
            &sb,
            &JsonAssert {
                contains: Some("smo".into()),
                ..ja("$.name")
            },
            &mut f,
        );
        check_json(
            &sb,
            &JsonAssert {
                exists: Some(false),
                ..ja("$.missing")
            },
            &mut f,
        );
        assert!(f.is_empty(), "{f:?}");
        check_json(
            &sb,
            &JsonAssert {
                equals: Some(serde_json::json!("fail")),
                ..ja("$.status")
            },
            &mut f,
        );
        check_json(
            &sb,
            &JsonAssert {
                exists: Some(true),
                ..ja("$.missing")
            },
            &mut f,
        );
        check_json(
            &sb,
            &JsonAssert {
                value_type: Some("string".into()),
                ..ja("$.steps[5]")
            },
            &mut f,
        );
        check_json(
            &sb,
            &JsonAssert {
                min_len: Some(3),
                ..ja("$.tags")
            },
            &mut f,
        );
        check_json(
            &sb,
            &JsonAssert {
                file: "bad.json".into(),
                ..ja("$")
            },
            &mut f,
        );
        check_json(
            &sb,
            &JsonAssert {
                file: "absent.json".into(),
                ..ja("$")
            },
            &mut f,
        );
        assert_eq!(f.len(), 6, "{f:?}");
    }

    #[test]
    fn timeout_step_fails_when_the_process_already_exited() {
        let sb = Sandbox::create(None).unwrap();
        let expect = Expect {
            timeout: true,
            ..Expect::default()
        };
        // Exited with a status, but a leftover child held its pipe, so timed_out is set.
        let exited = Observed {
            exit_code: Some(3),
            timed_out: true,
            stdout: String::new(),
            stderr: String::new(),
        };
        assert!(!check_step(&sb, &expect, &exited).is_empty());
        // Still running at the timeout: killed, so there is no exit code.
        let running = Observed {
            exit_code: None,
            timed_out: true,
            stdout: String::new(),
            stderr: String::new(),
        };
        assert!(check_step(&sb, &expect, &running).is_empty());
    }

    #[test]
    fn tree_not_contains_fails_on_a_missing_path() {
        let sb = Sandbox::create(None).unwrap();
        let missing = FileAssert {
            path: "absent".into(),
            root: Root::Sandbox,
            exists: None,
            kind: None,
            contains: Vec::new(),
            not_contains: Vec::new(),
            tree_not_contains: vec!["needle".into()],
        };
        let mut failures = Vec::new();
        check_file(&sb, &missing, &mut failures);
        assert_eq!(failures.len(), 1, "{failures:?}");

        std::fs::write(sb.root().join("present.txt"), "haystack").unwrap();
        let present = FileAssert {
            path: "present.txt".into(),
            ..missing
        };
        let mut ok = Vec::new();
        check_file(&sb, &present, &mut ok);
        assert!(ok.is_empty(), "{ok:?}");
    }
}
