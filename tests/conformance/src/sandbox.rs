//! The per-case sandbox: a fresh temp directory holding the project (the working directory of
//! every step) and a private home, XDG and temp tree, plus the isolated environment.

use std::fs;
use std::path::{Path, PathBuf};

use crate::case::{Root, Setup};

/// Proxy address that refuses connections, so any network attempt fails fast (no network).
pub const DEAD_PROXY: &str = "http://127.0.0.1:9";

/// Terminal width given to implementations so that wrapped console output is deterministic.
pub const COLUMNS: &str = "200";

/// Host variables passed through unchanged when present (needed to start programs at all).
#[cfg(windows)]
const PASSTHROUGH: &[&str] = &[
    "PATH",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "PATHEXT",
    "COMSPEC",
];
#[cfg(not(windows))]
const PASSTHROUGH: &[&str] = &["PATH"];

/// A sandbox on disk; removed when dropped unless kept.
#[derive(Debug)]
pub struct Sandbox {
    dir: tempfile::TempDir,
    root: PathBuf,
}

impl Sandbox {
    /// Create the directory tree below `parent` (or the system temp dir).
    pub fn create(parent: Option<&Path>) -> std::io::Result<Self> {
        let mut b = tempfile::Builder::new();
        b.prefix("argus-conf-");
        let dir = match parent {
            Some(p) => b.tempdir_in(p)?,
            None => b.tempdir()?,
        };
        let root = dir.path().to_path_buf();
        for sub in [
            "project",
            "home",
            "xdg/config",
            "xdg/data",
            "xdg/state",
            "xdg/cache",
            "tmp",
        ] {
            fs::create_dir_all(root.join(sub))?;
        }
        // Windows-style per-user roots live under home so they are covered by `root: home`.
        for sub in ["home/AppData/Roaming", "home/AppData/Local"] {
            fs::create_dir_all(root.join(sub))?;
        }
        Ok(Self { dir, root })
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    pub fn project(&self) -> PathBuf {
        self.root.join("project")
    }

    pub fn home(&self) -> PathBuf {
        self.root.join("home")
    }

    /// Resolve an assertion root.
    pub fn resolve_root(&self, root: Root) -> PathBuf {
        match root {
            Root::Project => self.project(),
            Root::Sandbox => self.root.clone(),
            Root::Home => self.home(),
            Root::XdgConfig => self.root.join("xdg/config"),
            Root::XdgData => self.root.join("xdg/data"),
            Root::XdgState => self.root.join("xdg/state"),
            Root::XdgCache => self.root.join("xdg/cache"),
        }
    }

    /// The spellings of the sandbox root that output normalization should hide.
    pub fn path_spellings(&self) -> Vec<String> {
        let mut v = vec![self.root.display().to_string()];
        if let Ok(c) = fs::canonicalize(&self.root) {
            let c = c.display().to_string();
            // Windows canonical paths carry a verbatim prefix that programs rarely print.
            let c = c.strip_prefix(r"\\?\").map(str::to_owned).unwrap_or(c);
            if !v.contains(&c) {
                v.push(c);
            }
        }
        v
    }

    /// Keep the directory on disk (for `--keep`); returns its path.
    pub fn keep(self) -> PathBuf {
        self.dir.keep()
    }

    /// The isolated base environment: nothing from the host except [`PASSTHROUGH`], private
    /// home/XDG/temp directories, an unusable proxy, plain non-interactive output.
    pub fn base_env(&self) -> Vec<(String, String)> {
        let s = |p: PathBuf| p.display().to_string();
        let mut env: Vec<(String, String)> = PASSTHROUGH
            .iter()
            .filter_map(|k| std::env::var(k).ok().map(|v| ((*k).to_owned(), v)))
            .collect();
        let home = self.home();
        let tmp = self.root.join("tmp");
        let pairs: Vec<(&str, String)> = vec![
            ("HOME", s(home.clone())),
            ("USERPROFILE", s(home.clone())),
            ("APPDATA", s(home.join("AppData/Roaming"))),
            ("LOCALAPPDATA", s(home.join("AppData/Local"))),
            ("XDG_CONFIG_HOME", s(self.root.join("xdg/config"))),
            ("XDG_DATA_HOME", s(self.root.join("xdg/data"))),
            ("XDG_STATE_HOME", s(self.root.join("xdg/state"))),
            ("XDG_CACHE_HOME", s(self.root.join("xdg/cache"))),
            ("XDG_RUNTIME_DIR", s(tmp.clone())),
            ("TMPDIR", s(tmp.clone())),
            ("TMP", s(tmp.clone())),
            ("TEMP", s(tmp)),
            ("HTTP_PROXY", DEAD_PROXY.into()),
            ("HTTPS_PROXY", DEAD_PROXY.into()),
            ("ALL_PROXY", DEAD_PROXY.into()),
            ("http_proxy", DEAD_PROXY.into()),
            ("https_proxy", DEAD_PROXY.into()),
            ("all_proxy", DEAD_PROXY.into()),
            ("NO_PROXY", String::new()),
            ("no_proxy", String::new()),
            ("LANG", "C.UTF-8".into()),
            ("LC_ALL", "C.UTF-8".into()),
            ("PYTHONIOENCODING", "utf-8".into()),
            ("PYTHONUTF8", "1".into()),
            ("PYTHONDONTWRITEBYTECODE", "1".into()),
            ("PYTHONNOUSERSITE", "1".into()),
            ("NO_COLOR", "1".into()),
            ("TERM", "dumb".into()),
            ("COLUMNS", COLUMNS.into()),
            ("LINES", "50".into()),
            ("CI", "1".into()),
        ];
        env.extend(pairs.into_iter().map(|(k, v)| (k.to_owned(), v)));
        env
    }

    /// Substitute `${PROJECT}`, `${SANDBOX}` and `${HOME}` in a case string.
    pub fn expand(&self, s: &str) -> String {
        s.replace("${PROJECT}", &self.project().display().to_string())
            .replace("${SANDBOX}", &self.root.display().to_string())
            .replace("${HOME}", &self.home().display().to_string())
    }

    /// Materialise the case's setup into the project directory. `repo_root` resolves `from`.
    pub fn apply_setup(&self, setup: &Setup, repo_root: &Path) -> Result<(), String> {
        let project = self.project();
        for d in &setup.dirs {
            fs::create_dir_all(project.join(d)).map_err(|e| format!("setup dir {d:?}: {e}"))?;
        }
        for f in &setup.files {
            let dest = project.join(&f.path);
            if let Some(parent) = dest.parent() {
                fs::create_dir_all(parent).map_err(|e| format!("setup {:?}: {e}", f.path))?;
            }
            let bytes = match (&f.content, &f.from) {
                (Some(c), _) => c.clone().into_bytes(),
                (None, Some(from)) => fs::read(repo_root.join(from))
                    .map_err(|e| format!("setup {:?} from {from:?}: {e}", f.path))?,
                (None, None) => return Err(format!("setup {:?}: no content", f.path)),
            };
            fs::write(&dest, bytes).map_err(|e| format!("setup {:?}: {e}", f.path))?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::case::SetupFile;

    #[test]
    fn layout_env_and_cleanup() {
        let sb = Sandbox::create(None).unwrap();
        let root = sb.root().to_path_buf();
        assert!(sb.project().is_dir() && sb.home().is_dir());
        let env = sb.base_env();
        let get = |k: &str| env.iter().find(|(n, _)| n == k).map(|(_, v)| v.clone());
        assert_eq!(get("HOME").unwrap(), sb.home().display().to_string());
        assert!(
            get("XDG_DATA_HOME")
                .unwrap()
                .starts_with(&root.display().to_string())
        );
        assert_eq!(get("HTTPS_PROXY").unwrap(), DEAD_PROXY);
        assert_eq!(get("NO_PROXY").unwrap(), "");
        // Host credentials never pass through.
        assert!(
            env.iter()
                .all(|(k, _)| !k.ends_with("_API_KEY") && !k.ends_with("_TOKEN")),
            "no provider key or token is passed through"
        );
        drop(sb);
        assert!(!root.exists(), "sandbox removed on drop");
    }

    #[test]
    fn setup_materialises_inline_and_golden_files() {
        let repo = tempfile::tempdir().unwrap();
        fs::create_dir_all(repo.path().join("tests/golden/x")).unwrap();
        fs::write(repo.path().join("tests/golden/x/a.json"), b"{}").unwrap();
        let sb = Sandbox::create(None).unwrap();
        let setup = Setup {
            dirs: vec!["empty/dir".into()],
            files: vec![
                SetupFile {
                    path: ".argus/c.yaml".into(),
                    content: Some("k: v\n".into()),
                    from: None,
                },
                SetupFile {
                    path: "g.json".into(),
                    content: None,
                    from: Some("tests/golden/x/a.json".into()),
                },
            ],
        };
        sb.apply_setup(&setup, repo.path()).unwrap();
        assert_eq!(
            fs::read_to_string(sb.project().join(".argus/c.yaml")).unwrap(),
            "k: v\n"
        );
        assert_eq!(fs::read(sb.project().join("g.json")).unwrap(), b"{}");
        assert!(sb.project().join("empty/dir").is_dir());
        let missing = Setup {
            dirs: vec![],
            files: vec![SetupFile {
                path: "m".into(),
                content: None,
                from: Some("tests/golden/none".into()),
            }],
        };
        assert!(sb.apply_setup(&missing, repo.path()).is_err());
    }

    #[test]
    fn placeholders_expand() {
        let sb = Sandbox::create(None).unwrap();
        let s = sb.expand("${PROJECT}/a ${HOME}");
        assert!(s.starts_with(&sb.project().display().to_string()));
        assert!(s.ends_with(&sb.home().display().to_string()));
    }
}
