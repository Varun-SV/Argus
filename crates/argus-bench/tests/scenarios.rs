//! Every committed scenario under bench/scenarios/ parses (work package BENCH, B6). Runs on
//! every operating system.

use std::path::{Path, PathBuf};

#[test]
fn committed_scenarios_parse() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../bench/scenarios");
    let mut files: Vec<PathBuf> = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|x| x == "toml"))
        .collect();
    files.sort();
    assert!(
        files.len() >= 6,
        "expected committed scenarios in {}",
        dir.display()
    );
    for file in &files {
        let s = argus_bench::scenario::Scenario::load(file)
            .unwrap_or_else(|e| panic!("{}: {e}", file.display()));
        if s.name.starts_with("argus-next-") {
            let pending = s.pending.as_deref().unwrap_or_default();
            assert!(
                pending.starts_with("needs argus-next feature"),
                "{} must be pending",
                s.name
            );
        }
        let stem = file.file_stem().unwrap().to_str().unwrap();
        assert_eq!(stem, s.name, "file name and scenario name differ");
    }
}
