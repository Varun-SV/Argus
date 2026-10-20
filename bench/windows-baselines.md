# Windows baselines for P0 (measured on a Windows machine)

Specification §12 P0 says: **"Measure the desktop-app and guest-runtime baselines on
Windows."** Specification §11 lists three baselines as "to be measured": the desktop app idle,
the desktop app during a run, and the size of the Capsule guest runtime (PyInstaller bundle).
None of them can be measured in the Linux cloud sandbox where the harness was built (no
desktop, no WebView2, no Windows). This page gives the exact steps for a person on a Windows
machine. The results replace the "blocked here" rows in
[docs/rearchitecture/benchmarks.md](../docs/rearchitecture/benchmarks.md#windows-baselines-blocked-here).

All commands are PowerShell, run from the root of an Argus checkout. Nothing here needs
administrator rights except installing the build tools.

## 0. Machine and preparation

- **Machine:** Windows 11 x64 (the §11 gate platform), on AC power, with no other heavy
  programs running. Close other WebView2 apps (Teams, new Outlook, widgets) if you can: they
  do not enter the measured tree, but they compete for memory and CPU.
- **Tools:** [rustup](https://rustup.rs) with the default MSVC toolchain (it offers to install
  the Visual Studio C++ build tools), Python 3.12 x64 from python.org (the `py` launcher),
  and Git. The WebView2 runtime is part of Windows 11.
- Write down the Windows version (`winver`), CPU model, RAM, and whether the window was on a
  high-DPI display. They go into labels below.

```powershell
# From the root of the Argus checkout.
cargo build --release -p argus-bench
$bench = (Resolve-Path .\target\release\argus-bench.exe).Path

# A dedicated virtual environment with the CURRENT Python desktop app.
py -3.12 -m venv "$env:TEMP\argus-bench-venv"
$py = "$env:TEMP\argus-bench-venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install "argus-app-testing[gui]"     # or: & $py -m pip install ".[gui]" for this checkout
$ver = & $py -c "import argus; print(argus.__version__)"
$pyver = & $py -c "import platform; print(platform.python_version())"

# Check the scenario files parse.
& $bench check bench\scenarios\windows-desktop-idle.toml
```

## 1. Desktop app idle (window open, no job): run mode

The scenario `bench/scenarios/windows-desktop-idle.toml` starts `argus-gui` in a fresh copy of
`bench/fixtures/desktop-project` (so the app opens that project directly) with a fresh desktop
state directory, samples the whole process tree every 50 ms for 60 seconds, ignores the first
15 seconds (start-up), then terminates the tree. It does this five times.

```powershell
& $bench run --scenario bench\scenarios\windows-desktop-idle.toml --python $py --runs 5 `
    --label argus=$ver --label python=$pyver --label windows="<winver, e.g. 11 24H2 26100>" `
    --out bench\results\baseline-python-windows-x86_64-desktop-idle.json
```

During each run: **do not touch the mouse or keyboard**, and leave the Argus window visible
(not minimized, not covered). A console window from the launcher appears as well; leave it.
Each run takes about one minute.

Check the table that is printed:

- `status ok` and five counted runs. A run marked `failed` with "exited ... before the
  measurement duration" means the app closed by itself; look at the printed output tail.
- The "peak by process name" list must contain the launcher (`argus-gui.exe`), Python
  (`python.exe` or `pythonw.exe`) **and** `msedgewebview2.exe` entries. If no
  `msedgewebview2.exe` entry appears, the WebView2 processes were not started as children of
  the app on this machine; measure with section 3 (attach) instead and give both
  `--name argus-gui --name msedgewebview2` (note that the second name then also matches
  WebView2 processes of other apps; close them first).

The **backend** number for the §11 comparison ("backend ≤ 30 MB") is the sum of the launcher
and Python entries; the **webview** number is the `msedgewebview2.exe` entry.

## 2. Desktop app during a run: attach mode

The fixture project holds `long-run-windows.test.yaml`, a CLI test that keeps a job active for
about 30 seconds (it runs `ping -n 31 127.0.0.1`) and makes no model call. While the job runs,
the current desktop UI polls the backend every 150 ms; this measurement records what that
costs.

```powershell
Remove-Item -Recurse -Force "$env:TEMP\argus-bench-desktop", "$env:TEMP\argus-bench-desktop-state" -ErrorAction SilentlyContinue
Copy-Item -Recurse bench\fixtures\desktop-project "$env:TEMP\argus-bench-desktop"
$env:ARGUS_GUI_STATE_DIR = "$env:TEMP\argus-bench-desktop-state"
Start-Process -FilePath "$env:TEMP\argus-bench-venv\Scripts\argus-gui.exe" -WorkingDirectory "$env:TEMP\argus-bench-desktop"
```

Wait until the window shows the project (about 10 seconds). Then start the sampler, and
**within five seconds** start the test in the app: click the **Run all tests** suggestion (or
type `run all tests` in the message box and confirm if the app asks). The project has only
this one test.

```powershell
$i = 1   # repetition number, 1 to 5
& $bench attach --name argus-gui --duration 45s --measure-after 3s `
    --label phase=during-run --label repetition=$i --label argus=$ver --label python=$pyver `
    --out "bench\results\desktop-run-$i.json"
```

Leave the window visible and do not interact until the sampler prints its table (45 s). The
run should show as passed in the app. Close the app with its window's close button. Repeat
the whole section with `$i = 2` ... `$i = 5`: attach mode records one run per file, and the
baseline is the **median** of the five "peak tree MiB" values (and of the per-name entries).

Optional, for comparison with section 1: with the app open and idle, run
`& $bench attach --name argus-gui --duration 60s --label phase=idle-attach --out ...` once.

## 3. Attach mode in general

`argus-bench attach` samples any running tree:

```powershell
& $bench attach --name argus-gui --duration 60s --out result.json     # by name (top-most matches)
& $bench attach --pid 12345 --duration 60s --out result.json          # by PID (Task Manager, Details tab)
```

Names are matched case-insensitively, `.exe` optional. Each top-most matching process is a
root, and all its descendants are measured. The metric on Windows is the **working set** of
each process (`GetProcessMemoryInfo` `WorkingSetSize`), summed over the tree.

## 4. Capsule guest runtime (PyInstaller bundle) size

The current guest runtime is built with `argus-guest-runtime-build` (see
[docs/os-environment-provisioning.md](../docs/os-environment-provisioning.md)). Build it in a
separate environment with the adapters a Windows guest uses, then record the archive size and
the unpacked size.

```powershell
py -3.12 -m venv "$env:TEMP\argus-guest-build-venv"
$gpy = "$env:TEMP\argus-guest-build-venv\Scripts\python.exe"
& $gpy -m pip install "argus-app-testing[guest-runtime-build,windows,browser]"
$gver = & $gpy -c "import argus; print(argus.__version__)"
& "$env:TEMP\argus-guest-build-venv\Scripts\argus-guest-runtime-build.exe" "$env:TEMP\argus-guest-win.zip" --runtime-version 0.0.0-bench

$zip = Get-Item "$env:TEMP\argus-guest-win.zip"
Expand-Archive $zip.FullName -DestinationPath "$env:TEMP\argus-guest-win" -Force
$unpacked = (Get-ChildItem -Recurse -File "$env:TEMP\argus-guest-win" | Measure-Object Length -Sum).Sum
$exe = (Get-Item "$env:TEMP\argus-guest-win\argus-guest.exe").Length
"argus=$gver zip_bytes=$($zip.Length) unpacked_bytes=$unpacked entrypoint_bytes=$exe"
# The five largest parts of the bundle:
Get-ChildItem "$env:TEMP\argus-guest-win\_internal" | ForEach-Object {
    $bytes = if ($_.PSIsContainer) { (Get-ChildItem -Recurse -File $_.FullName | Measure-Object Length -Sum).Sum } else { $_.Length }
    [pscustomobject]@{ Name = $_.Name; Bytes = $bytes }
} | Sort-Object Bytes -Descending | Select-Object -First 5
```

Record the printed line and the five largest parts. If the extras list differs from what your
guests use, note the extras you installed. (For comparison, the Linux bundle built in the
sandbox is in benchmarks.md.)

Optional, inside a running Windows Capsule guest: copy `argus-bench.exe` into the guest and
run `argus-bench.exe attach --name argus-guest --duration 60s --out guest-idle.json` while
the guest agent is idle. This is the baseline for the §11 target "idle ≤ 10 MB".

## 5. Handing the results back

1. Commit the JSON files under `bench/results/` (they contain no host name, user name or
   path; check the `labels` you added).
2. Combine the five during-run files into one set:
   `& $bench merge --description "Python desktop app during a run, Windows 11 x64" --out bench\results\baseline-python-windows-x86_64-desktop-run.json (1..5 | ForEach-Object { "bench\results\desktop-run-$_.json" })`,
   then delete the five single files.
3. Print the tables with `& $bench show <file>` and copy the medians, plus the guest bundle
   sizes, into the "Windows baselines" table of `docs/rearchitecture/benchmarks.md`, replacing
   "blocked here".
