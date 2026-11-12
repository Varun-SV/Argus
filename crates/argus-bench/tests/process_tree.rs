//! Integration tests against real processes (work package BENCH, B8). Unix only: they use
//! `sh` and `sleep`. The Windows and macOS builds are covered by the unit tests with the fake
//! process source and by `cargo check` for those targets.

#![cfg(unix)]
// Helpers below are test code: a panic is the failure report (clippy.toml allows unwrap only
// inside #[test] functions themselves).
#![allow(clippy::unwrap_used)]

use std::path::{Path, PathBuf};
use std::process::{Command, Output};
use std::time::{Duration, Instant};

use argus_bench::process::{ProcessSource, SysinfoSource};
use argus_bench::tree::TreeTracker;
use serde_json::Value;

const BIN: &str = env!("CARGO_BIN_EXE_argus-bench");

/// A throw-away "repository": a temp dir with a bench/ directory for root discovery.
fn repo() -> tempfile::TempDir {
    let dir = tempfile::tempdir().unwrap();
    std::fs::create_dir_all(dir.path().join("bench/scenarios")).unwrap();
    dir
}

fn write_scenario(repo: &Path, name: &str, body: &str) -> PathBuf {
    let path = repo.join("bench/scenarios").join(format!("{name}.toml"));
    std::fs::write(
        &path,
        format!("name = \"{name}\"\ndescription = \"test\"\n{body}"),
    )
    .unwrap();
    path
}

fn bench(args: &[&str]) -> (Output, Value) {
    let output = Command::new(BIN).args(args).output().unwrap();
    let json: Value = serde_json::from_slice(&output.stdout).unwrap_or_else(|e| {
        panic!(
            "no JSON on stdout ({e}); stdout:\n{}\nstderr:\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )
    });
    (output, json)
}

fn as_u64(v: &Value) -> u64 {
    v.as_u64().unwrap_or_else(|| panic!("not a u64: {v}"))
}

#[test]
fn tracker_sees_shell_and_both_background_children() {
    let mut child = Command::new("sh")
        .args(["-c", "sleep 3 & sleep 3 & wait"])
        .spawn()
        .unwrap();
    let mut source = SysinfoSource::new();
    let mut tracker = TreeTracker::new(&[child.id()]);
    let deadline = Instant::now() + Duration::from_secs(5);
    let mut best = None;
    while Instant::now() < deadline {
        let sample = tracker.observe(&source.snapshot());
        let sleeps: Vec<_> = sample
            .processes
            .iter()
            .filter(|p| p.name == "sleep")
            .collect();
        if sleeps.len() == 2 {
            best = Some(sample.clone());
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    let sample = best.expect("never saw sh with two sleep children");
    let children: u64 = sample
        .processes
        .iter()
        .filter(|p| p.pid != child.id())
        .map(|p| p.memory_bytes)
        .sum();
    let all: u64 = sample.processes.iter().map(|p| p.memory_bytes).sum();
    assert_eq!(sample.total_bytes, all);
    assert!(sample.total_bytes >= children);
    assert!(children > 0, "children reported no memory");
    assert!(sample.roots_alive);
    // Clean up the whole tree.
    let ids: Vec<_> = sample
        .processes
        .iter()
        .map(argus_bench::process::ProcInfo::id)
        .collect();
    assert_eq!(source.terminate(&ids), 3);
    let _ = child.wait();
}

#[test]
fn run_measures_peak_at_least_the_children_seen() {
    let repo = repo();
    let file = write_scenario(
        repo.path(),
        "tree",
        "command = [\"sh\", \"-c\", \"sleep 1 & sleep 1 & wait\"]\nwarmup = 0\ntimeout = \"30s\"\n",
    );
    let (output, json) = bench(&[
        "run",
        "--scenario",
        file.to_str().unwrap(),
        "--runs",
        "2",
        "--interval-ms",
        "20",
        "--json",
        "--timestamp",
        "2026-10-08T00:00:00Z",
    ]);
    assert!(output.status.success(), "{json}");
    assert_eq!(json["schema"], "argus-bench-result-v1");
    assert_eq!(json["status"], "ok");
    assert_eq!(json["created_utc"], "2026-10-08T00:00:00Z");
    assert_eq!(json["metric"]["name"], "rss");
    assert_eq!(json["metric"]["interval_ms"], 20);
    assert_eq!(json["scenario"]["file"], "bench/scenarios/tree.toml");
    for run in json["runs"].as_array().unwrap() {
        let memory = &run["memory"];
        let peak = as_u64(&memory["peak_tree_bytes"]);
        let by_name = memory["by_name"].as_array().unwrap();
        let sleep = by_name
            .iter()
            .find(|n| n["name"] == "sleep")
            .expect("no sleep");
        let sh = by_name
            .iter()
            .find(|n| n["name"] != "sleep")
            .expect("no shell");
        assert_eq!(as_u64(&sleep["max_concurrent"]), 2);
        assert!(as_u64(&memory["distinct_processes"]) >= 3);
        // The two sleeps are summed in one sample; the peak sample also holds the shell.
        assert!(peak >= as_u64(&sleep["peak_bytes"]), "{memory}");
        assert!(peak > as_u64(&sleep["peak_bytes"]).max(as_u64(&sh["peak_bytes"])));
        let wall = as_u64(&run["wall_us"]);
        assert!((900_000..10_000_000).contains(&wall), "wall {wall}");
    }
    let summary = &json["summary"];
    assert_eq!(summary["runs"], 2);
    assert!(as_u64(&summary["peak_tree_bytes"]["median"]) > 0);
    // Environment metadata is limited to the documented fields.
    let env = json["environment"].as_object().unwrap();
    let keys: Vec<&str> = env.keys().map(String::as_str).collect();
    assert_eq!(
        keys,
        [
            "arch",
            "logical_cpus",
            "os",
            "os_name",
            "os_version",
            "total_ram_bytes"
        ]
    );
}

#[test]
fn workdir_env_setup_and_teardown_are_applied() {
    let repo = repo();
    std::fs::create_dir_all(repo.path().join("bench/fixtures/proj/sub")).unwrap();
    std::fs::write(repo.path().join("bench/fixtures/proj/sub/f.txt"), "x").unwrap();
    let body = r#"
command = ["sh", "-c", "test -f sub/f.txt && test -f made-by-setup && test -d extra && test \"$FOO\" = \"bar-{OS_FAMILY}\" && test -z \"${GONE+set}\""]
warmup = 0
[workdir]
copy_from = "bench/fixtures/proj"
create_dirs = ["extra"]
[env]
FOO = "bar-{OS_FAMILY}"
GONE = ""
"#;
    let body = format!(
        "setup = [[\"sh\", \"-c\", \"touch made-by-setup\"]]\nteardown = [[\"sh\", \"-c\", \"rm made-by-setup\"]]\n{body}"
    );
    let file = write_scenario(repo.path(), "env", &body);
    let output = Command::new(BIN)
        .args([
            "run",
            "--scenario",
            file.to_str().unwrap(),
            "--runs",
            "1",
            "--json",
        ])
        .env("GONE", "1")
        .output()
        .unwrap();
    let json: Value = serde_json::from_slice(&output.stdout).unwrap();
    assert_eq!(json["status"], "ok", "{json}");
    // The fixture in the repository is untouched.
    assert!(
        !repo
            .path()
            .join("bench/fixtures/proj/made-by-setup")
            .exists()
    );
}

#[test]
fn unexpected_exit_fails_with_exit_code_one() {
    let repo = repo();
    let file = write_scenario(
        repo.path(),
        "fail",
        "command = [\"sh\", \"-c\", \"exit 3\"]\nwarmup = 0\n",
    );
    let out_file = repo.path().join("result.json");
    let (output, json) = bench(&[
        "run",
        "--scenario",
        file.to_str().unwrap(),
        "--runs",
        "1",
        "--json",
        "--out",
        out_file.to_str().unwrap(),
    ]);
    assert_eq!(output.status.code(), Some(1));
    assert_eq!(json["status"], "failed");
    assert_eq!(json["runs"][0]["exit_code"], 3);
    assert!(json["summary"].is_null());
    let written: Value = serde_json::from_slice(&std::fs::read(&out_file).unwrap()).unwrap();
    assert_eq!(written, json);
}

#[test]
fn timeout_terminates_the_tree() {
    let repo = repo();
    let file = write_scenario(
        repo.path(),
        "slow",
        "command = [\"sh\", \"-c\", \"sleep 30 & sleep 30 & wait\"]\nwarmup = 0\ntimeout = \"1s\"\n",
    );
    let started = Instant::now();
    let (output, json) = bench(&[
        "run",
        "--scenario",
        file.to_str().unwrap(),
        "--runs",
        "1",
        "--json",
    ]);
    assert_eq!(output.status.code(), Some(1));
    assert!(started.elapsed() < Duration::from_secs(20));
    assert_eq!(json["runs"][0]["timed_out"], true);
    assert!(as_u64(&json["runs"][0]["memory"]["distinct_processes"]) >= 3);
}

#[test]
fn pending_and_skipped_scenarios_exit_zero() {
    let repo = repo();
    let pending = write_scenario(
        repo.path(),
        "pend",
        "command = [\"{ARGUS_NEXT}\", \"--help\"]\npending = \"needs argus-next feature: x\"\n",
    );
    let (output, json) = bench(&["run", "--scenario", pending.to_str().unwrap(), "--json"]);
    assert!(output.status.success());
    assert_eq!(json["status"], "pending");
    assert!(json["runs"].as_array().unwrap().is_empty());

    let skipped = write_scenario(
        repo.path(),
        "skip",
        "command = [\"sh\", \"-c\", \"exit 0\"]\n[skip_unless]\ncommand = [\"sh\", \"-c\", \"exit 1\"]\nreason = \"no widget\"\n",
    );
    let (output, json) = bench(&["run", "--scenario", skipped.to_str().unwrap(), "--json"]);
    assert!(output.status.success());
    assert_eq!(json["status"], "skipped");
    assert!(
        json["status_reason"]
            .as_str()
            .unwrap()
            .contains("no widget")
    );
}

#[test]
fn attach_samples_a_running_tree() {
    let mut child = Command::new("sh")
        .args(["-c", "sleep 5 & wait"])
        .spawn()
        .unwrap();
    let pid = child.id().to_string();
    let (output, json) = bench(&[
        "attach",
        "--pid",
        &pid,
        "--duration",
        "600ms",
        "--interval-ms",
        "50",
        "--json",
        "--label",
        "purpose=test",
    ]);
    let _ = Command::new("pkill").args(["-P", &pid]).status();
    let _ = child.kill();
    let _ = child.wait();
    assert!(output.status.success(), "{json}");
    assert_eq!(json["mode"], "attach");
    assert_eq!(json["attach"]["selector"], "pid");
    assert_eq!(json["labels"]["purpose"], "test");
    assert!(as_u64(&json["runs"][0]["memory"]["samples"]) >= 5);
    assert!(as_u64(&json["summary"]["distinct_processes"]["median"]) >= 2);
}

#[test]
fn merge_and_show_round_trip() {
    let repo = repo();
    let a = write_scenario(
        repo.path(),
        "one",
        "command = [\"sh\", \"-c\", \"exit 0\"]\nwarmup = 0\n",
    );
    let b = write_scenario(
        repo.path(),
        "two",
        "command = [\"x\"]\npending = \"needs y\"\n",
    );
    let ra = repo.path().join("a.json");
    let rb = repo.path().join("b.json");
    for (scenario, out) in [(&a, &ra), (&b, &rb)] {
        let status = Command::new(BIN)
            .args([
                "run",
                "--scenario",
                scenario.to_str().unwrap(),
                "--runs",
                "1",
            ])
            .args(["--out", out.to_str().unwrap()])
            .output()
            .unwrap()
            .status;
        assert!(status.success());
    }
    let set = repo.path().join("set.json");
    let output = Command::new(BIN)
        .args([
            "merge",
            "--description",
            "two results",
            "--out",
            set.to_str().unwrap(),
        ])
        .args([ra.to_str().unwrap(), rb.to_str().unwrap()])
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    let json: Value = serde_json::from_slice(&std::fs::read(&set).unwrap()).unwrap();
    assert_eq!(json["schema"], "argus-bench-result-set-v1");
    assert_eq!(json["results"].as_array().unwrap().len(), 2);
    // One sample per run for `exit 0`: the result carries the few-samples warning.
    assert!(
        !json["results"][0]["warnings"]
            .as_array()
            .unwrap()
            .is_empty()
    );
    let shown = Command::new(BIN)
        .args(["show", set.to_str().unwrap()])
        .output()
        .unwrap();
    let text = String::from_utf8(shown.stdout).unwrap();
    assert!(text.contains("scenario one") && text.contains("scenario two | status pending"));
}
