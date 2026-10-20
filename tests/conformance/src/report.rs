//! The JSON report (`argus-conformance-report-v1`) and the human summary.

use serde::Serialize;

use crate::runner::{CaseResult, Outcome};

/// Schema identifier of the JSON report.
pub const REPORT_SCHEMA: &str = "argus-conformance-report-v1";

/// Outcome counts.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
pub struct Summary {
    pub total: usize,
    pub pass: usize,
    pub fail: usize,
    pub pending: usize,
    pub known_gap: usize,
    pub skipped: usize,
    pub error: usize,
}

impl Summary {
    pub fn of(results: &[CaseResult]) -> Self {
        let mut s = Self {
            total: results.len(),
            ..Self::default()
        };
        for r in results {
            match r.outcome {
                Outcome::Pass => s.pass += 1,
                Outcome::Fail => s.fail += 1,
                Outcome::Pending => s.pending += 1,
                Outcome::KnownGap => s.known_gap += 1,
                Outcome::Skipped => s.skipped += 1,
                Outcome::Error => s.error += 1,
            }
        }
        s
    }

    /// Process exit code: non-zero only when a case failed or errored.
    pub fn exit_code(&self) -> i32 {
        i32::from(self.fail > 0 || self.error > 0)
    }

    pub fn line(&self) -> String {
        format!(
            "{} cases: {} pass, {} fail, {} pending, {} known-gap, {} skipped, {} error",
            self.total,
            self.pass,
            self.fail,
            self.pending,
            self.known_gap,
            self.skipped,
            self.error
        )
    }
}

/// The whole report.
#[derive(Debug, Clone, Serialize)]
pub struct Report<'a> {
    pub schema: &'static str,
    /// `python` or `rust`: which case statuses were applied.
    pub implementation: &'a str,
    /// The command used, as the executable's file name plus fixed arguments (no host paths).
    pub command: Vec<String>,
    pub platform: &'a str,
    pub filter: Option<&'a str>,
    pub summary: Summary,
    /// Sorted by case id.
    pub results: &'a [CaseResult],
}

/// Strip directories from the executable so a report never records host paths.
pub fn display_command(argv: &[String]) -> Vec<String> {
    argv.iter()
        .enumerate()
        .map(|(i, a)| {
            if i == 0 {
                std::path::Path::new(a)
                    .file_name()
                    .map_or_else(|| a.clone(), |n| n.to_string_lossy().into_owned())
            } else {
                a.clone()
            }
        })
        .collect()
}

/// Human-readable summary: one line per case, failure reasons indented, counts last.
pub fn human(results: &[CaseResult], verbose: bool) -> String {
    let mut out = String::new();
    for r in results {
        let mut line = format!("{:<10} {}", r.outcome.label(), r.id);
        if r.would_pass == Some(true) {
            line.push_str("  (assertions hold: can be promoted to active)");
        }
        out.push_str(line.trim_end());
        out.push('\n');
        let show_reasons = matches!(r.outcome, Outcome::Fail | Outcome::Error)
            || (verbose && !r.failures.is_empty());
        if show_reasons {
            for f in &r.failures {
                out.push_str("           - ");
                out.push_str(f);
                out.push('\n');
            }
        }
        if verbose {
            if let Some(n) = &r.note {
                out.push_str("           note: ");
                out.push_str(n);
                out.push('\n');
            }
        }
    }
    out.push_str(&Summary::of(results).line());
    out.push('\n');
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn r(id: &str, outcome: Outcome) -> CaseResult {
        CaseResult {
            id: id.into(),
            file: format!("{id}.json"),
            title: "t".into(),
            requirements: vec!["C-07".into()],
            declared: "active".into(),
            outcome,
            would_pass: None,
            note: None,
            failures: if outcome == Outcome::Fail {
                vec!["exit code: expected 0, got 2".into()]
            } else {
                vec![]
            },
            steps: vec![],
            duration_ms: 1,
        }
    }

    #[test]
    fn counts_and_exit_code() {
        let ok = [
            r("a", Outcome::Pass),
            r("b", Outcome::Pending),
            r("c", Outcome::KnownGap),
            r("d", Outcome::Skipped),
        ];
        let s = Summary::of(&ok);
        assert_eq!(
            (s.total, s.pass, s.pending, s.known_gap, s.skipped),
            (4, 1, 1, 1, 1)
        );
        assert_eq!(s.exit_code(), 0);
        assert_eq!(Summary::of(&[r("a", Outcome::Fail)]).exit_code(), 1);
        assert_eq!(Summary::of(&[r("a", Outcome::Error)]).exit_code(), 1);
    }

    #[test]
    fn human_summary_lists_failures() {
        let text = human(
            &[r("cli.a", Outcome::Pass), r("cli.b", Outcome::Fail)],
            false,
        );
        assert!(text.contains("PASS       cli.a\n"));
        assert!(text.contains("FAIL       cli.b\n           - exit code: expected 0, got 2\n"));
        assert!(
            text.ends_with("2 cases: 1 pass, 1 fail, 0 pending, 0 known-gap, 0 skipped, 0 error\n")
        );
    }

    #[test]
    fn report_json_shape_and_no_host_paths() {
        let results = [r("cli.a", Outcome::Pass)];
        let argv = vec![
            "/some/venv/bin/python".to_owned(),
            "-m".into(),
            "argus".into(),
        ];
        let rep = Report {
            schema: REPORT_SCHEMA,
            implementation: "python",
            command: display_command(&argv),
            platform: "linux",
            filter: None,
            summary: Summary::of(&results),
            results: &results,
        };
        let v = serde_json::to_value(&rep).unwrap();
        assert_eq!(v["schema"], REPORT_SCHEMA);
        assert_eq!(v["command"], serde_json::json!(["python", "-m", "argus"]));
        assert_eq!(v["summary"]["pass"], 1);
        assert_eq!(v["results"][0]["outcome"], "pass");
        assert!(v["results"][0].get("would_pass").is_none());
    }
}
