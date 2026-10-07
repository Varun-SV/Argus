//! Black-box checks of the `argus-next` preview binary.

use std::process::Command;

fn argus_next() -> Command {
    Command::new(env!("CARGO_BIN_EXE_argus-next"))
}

#[test]
fn version_prints_name_and_version() {
    let out = argus_next().arg("--version").output().expect("binary runs");
    assert!(out.status.success());
    let stdout = String::from_utf8(out.stdout).expect("utf-8");
    assert_eq!(
        stdout.trim(),
        format!("argus-next {}", env!("CARGO_PKG_VERSION"))
    );
}

#[test]
fn help_succeeds_and_mentions_preview() {
    let out = argus_next().arg("--help").output().expect("binary runs");
    assert!(out.status.success());
    let stdout = String::from_utf8(out.stdout).expect("utf-8");
    assert!(stdout.contains("Usage: argus-next"));
    assert!(stdout.contains("preview"));
}

#[test]
fn no_arguments_prints_banner() {
    let out = argus_next().output().expect("binary runs");
    assert!(out.status.success());
    let stdout = String::from_utf8(out.stdout).expect("utf-8");
    assert!(stdout.starts_with("argus-next "));
    assert!(stdout.contains("preview"));
}

#[test]
fn unknown_argument_exits_with_usage_error() {
    let out = argus_next().arg("run").output().expect("binary runs");
    // clap reports usage errors with exit code 2.
    assert_eq!(out.status.code(), Some(2));
}
