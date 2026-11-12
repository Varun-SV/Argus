//! Output normalizers. Assertions run on normalized text, so cases check information content
//! rather than terminal styling, machine-specific paths, clocks or build versions (C-07).

use std::sync::LazyLock;

use regex::Regex;

use crate::case::NormalizerSpec;

/// Normalizers that every case gets, in this order, before its own.
pub const DEFAULTS: &[&str] = &["strip_ansi", "crlf", "tmpdir"];

/// Every built-in normalizer name.
pub const BUILTIN_NAMES: &[&str] = &[
    "strip_ansi",
    "crlf",
    "tmpdir",
    "timestamps",
    "versions",
    "uuids",
    "durations",
];

/// Placeholder that replaces the sandbox root.
pub const TMP_PLACEHOLDER: &str = "<TMP>";

#[allow(clippy::expect_used)] // Constant patterns, covered by unit tests.
fn re(pattern: &str) -> Regex {
    Regex::new(pattern).expect("built-in normalizer pattern is valid")
}

// CSI sequences (colours, cursor movement), OSC sequences (titles, hyperlinks) and the
// two-byte escapes some terminals libraries emit.
static ANSI: LazyLock<Regex> = LazyLock::new(|| {
    re(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
});
static TIMESTAMP: LazyLock<Regex> = LazyLock::new(|| {
    re(concat!(
        r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?",
        r"|\b\d{8}[-T]\d{6}\b"
    ))
});
static VERSION: LazyLock<Regex> =
    LazyLock::new(|| re(r"\bv?\d+\.\d+\.\d+(?:[-.+][0-9A-Za-z][0-9A-Za-z.+-]*)?"));
static UUID: LazyLock<Regex> = LazyLock::new(|| {
    re(
        r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b|\b[0-9a-fA-F]{32,}\b",
    )
});
static DURATION: LazyLock<Regex> = LazyLock::new(|| re(r"\b\d+(?:\.\d+)?\s?(?:ms|s)\b"));

/// Applies a fixed list of normalizers for one case run.
#[derive(Debug, Clone)]
pub struct Normalizer {
    /// Sandbox paths (as printed by the implementation) replaced by `<TMP>`, longest first.
    tmp_paths: Vec<String>,
    steps: Vec<Op>,
}

#[derive(Debug, Clone)]
enum Op {
    Builtin(&'static str),
    Replace(Regex, String),
}

impl Normalizer {
    /// Build the default normalizers plus the case's own. `tmp_paths` are the spellings of the
    /// sandbox root to hide (for example the path and its canonical form).
    pub fn new(extra: &[NormalizerSpec], tmp_paths: &[String]) -> Result<Self, String> {
        let mut steps: Vec<Op> = Vec::new();
        let push_named = |name: &str, steps: &mut Vec<Op>| -> Result<(), String> {
            let found = BUILTIN_NAMES
                .iter()
                .find(|n| **n == name)
                .ok_or_else(|| format!("unknown normalizer {name:?}"))?;
            if !steps
                .iter()
                .any(|s| matches!(s, Op::Builtin(n) if n == found))
            {
                steps.push(Op::Builtin(found));
            }
            Ok(())
        };
        for d in DEFAULTS {
            push_named(d, &mut steps)?;
        }
        for spec in extra {
            match spec {
                NormalizerSpec::Named(n) => push_named(n, &mut steps)?,
                NormalizerSpec::Replace { regex, replace } => {
                    let r = Regex::new(regex).map_err(|e| format!("bad regex {regex:?}: {e}"))?;
                    steps.push(Op::Replace(r, replace.clone()));
                }
            }
        }
        let mut tmp: Vec<String> = Vec::new();
        for p in tmp_paths.iter().filter(|p| !p.is_empty()) {
            for variant in [p.clone(), p.replace('\\', "/"), p.replace('/', "\\")] {
                if !tmp.contains(&variant) {
                    tmp.push(variant);
                }
            }
        }
        tmp.sort_by_key(|p| std::cmp::Reverse(p.len()));
        Ok(Self {
            tmp_paths: tmp,
            steps,
        })
    }

    /// Normalize raw process output (decoded lossily as UTF-8).
    pub fn apply(&self, raw: &[u8]) -> String {
        let mut s = String::from_utf8_lossy(raw).into_owned();
        for op in &self.steps {
            s = match op {
                Op::Builtin(name) => self.builtin(name, &s),
                Op::Replace(r, rep) => r.replace_all(&s, rep.as_str()).into_owned(),
            };
        }
        s
    }

    fn builtin(&self, name: &str, s: &str) -> String {
        match name {
            "strip_ansi" => ANSI.replace_all(s, "").into_owned(),
            "crlf" => s.replace("\r\n", "\n"),
            "tmpdir" => {
                let mut out = s.to_owned();
                for p in &self.tmp_paths {
                    out = out.replace(p.as_str(), TMP_PLACEHOLDER);
                }
                out
            }
            "timestamps" => TIMESTAMP.replace_all(s, "<TIMESTAMP>").into_owned(),
            "versions" => VERSION.replace_all(s, "<VERSION>").into_owned(),
            "uuids" => UUID.replace_all(s, "<UUID>").into_owned(),
            "durations" => DURATION.replace_all(s, "<DURATION>").into_owned(),
            _ => s.to_owned(),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn named(names: &[&str]) -> Vec<NormalizerSpec> {
        names
            .iter()
            .map(|n| NormalizerSpec::Named((*n).to_owned()))
            .collect()
    }

    #[test]
    fn strips_ansi_and_crlf_by_default() {
        let n = Normalizer::new(&[], &[]).unwrap();
        let raw = b"\x1b[32m\xe2\x9c\x93\x1b[0m ok\r\n\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\\r\n";
        assert_eq!(n.apply(raw), "\u{2713} ok\nlink\n");
    }

    #[test]
    fn replaces_sandbox_paths_longest_first_including_canonical_forms() {
        let n = Normalizer::new(&[], &["/tmp/a".into(), "/private/tmp/a".into()]).unwrap();
        assert_eq!(
            n.apply(b"at /private/tmp/a/project and /tmp/a/home"),
            "at <TMP>/project and <TMP>/home"
        );
        let w = Normalizer::new(&[], &[r"C:\Users\x\Temp\r".into()]).unwrap();
        assert_eq!(w.apply(br"C:\Users\x\Temp\r\project"), r"<TMP>\project");
        assert_eq!(w.apply(b"C:/Users/x/Temp/r/project"), "<TMP>/project");
    }

    #[test]
    fn optional_normalizers() {
        let n = Normalizer::new(&named(&["timestamps", "uuids", "durations"]), &[]).unwrap();
        let s = n.apply(
            b"at 2026-10-08T09:43:01.123Z run 20261008-094301 id 123e4567-e89b-12d3-a456-426614174000 \
              tok 0123456789abcdef0123456789abcdef took 1.25s and 30 ms",
        );
        assert_eq!(
            s,
            "at <TIMESTAMP> run <TIMESTAMP> id <UUID> tok <UUID> took <DURATION> and <DURATION>"
        );
        let v = Normalizer::new(&named(&["versions"]), &[]).unwrap();
        assert_eq!(
            v.apply(b"argus, version 0.1.6.dev10+gacd72bc65"),
            "argus, version <VERSION>"
        );
        assert_eq!(v.apply(b"argus-next 0.0.0"), "argus-next <VERSION>");
        assert_eq!(v.apply(b"v1.2.3-rc.1 done"), "<VERSION> done");
    }

    #[test]
    fn versions_are_opt_in_so_addresses_survive_by_default() {
        let n = Normalizer::new(&[], &[]).unwrap();
        assert_eq!(n.apply(b"bind 127.0.0.1:5000"), "bind 127.0.0.1:5000");
    }

    #[test]
    fn custom_replacements_run_after_builtins() {
        let n = Normalizer::new(
            &[NormalizerSpec::Replace {
                regex: r"port \d+".into(),
                replace: "port N".into(),
            }],
            &["/s".into()],
        )
        .unwrap();
        assert_eq!(n.apply(b"/s/x port 5000"), "<TMP>/x port N");
        assert!(Normalizer::new(&named(&["nope"]), &[]).is_err());
    }

    #[test]
    fn invalid_utf8_is_decoded_lossily() {
        let n = Normalizer::new(&[], &[]).unwrap();
        assert_eq!(n.apply(b"a\xffb"), "a\u{fffd}b");
    }
}
