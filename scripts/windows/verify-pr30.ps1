<#
.SYNOPSIS
  Verifies Argus PR 30 on Windows: Python tests, Rust tests/build, bench scenarios, and an
  optional native media test. Logs go to %TEMP%\argus-verify-<timestamp>, never into the repo.
.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\windows\verify-pr30.ps1 -RepoPath C:\src\Argus
#>
[CmdletBinding()]
param(
    [string]$RepoPath = (Get-Location).Path,
    [string]$PythonExe = "python",
    [switch]$NativeMediaTest,
    [switch]$SkipRust,
    [switch]$SkipBench
)
$ErrorActionPreference = "Stop"

$Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$LogDir = Join-Path $env:TEMP ("argus-verify-" + $Stamp)
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$Results = New-Object System.Collections.ArrayList
$Abort = $false

# Double trailing backslashes so a drive-root path (C:\) survives Windows argument parsing.
function Q([string]$s) { $t = $s -replace '(\\+)$', '$1$1'; return '"' + $t + '"' }

function Add-Result {
    param([string]$Name, [string]$Status, [string]$Detail)
    [void]$Results.Add([pscustomobject]@{ Name = $Name; Status = $Status; Detail = $Detail })
    Write-Host ("[{0}] {1} {2}" -f $Status, $Name, $Detail)
}

# Runs a native command, streams and logs stdout+stderr, returns the exit code.
# ErrorActionPreference is relaxed inside, because Windows PowerShell 5.1 turns native
# stderr text into terminating errors when it is "Stop".
function Invoke-Native {
    param([string]$Exe, [string[]]$ArgList = @(), [string]$Log)
    ("> " + $Exe + " " + ($ArgList -join " ")) | Out-File -FilePath $Log -Encoding utf8 -Append
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = $null
    $code = 1
    try {
        & $Exe @ArgList 2>&1 | ForEach-Object { $s = "$_"; Write-Host $s; $s } |
            Out-File -FilePath $Log -Encoding utf8 -Append
        $code = $global:LASTEXITCODE
    } catch {
        ("ERROR: " + $_.Exception.Message) | Out-File -FilePath $Log -Encoding utf8 -Append
        $code = 1
    } finally {
        $ErrorActionPreference = $old
    }
    if ($null -eq $code) { $code = 1 }
    return [int]$code
}

function Get-PytestSummary {
    param([string]$Log)
    $m = Select-String -Path $Log -Pattern '(\d+ (passed|failed|errors?|skipped|deselected|xfailed|xpassed)|no tests ran).* in [0-9.]+s' |
        Select-Object -Last 1
    if (-not $m) { return "no pytest result line found" }
    $parts = @()
    foreach ($x in [regex]::Matches($m.Line, '(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)')) { $parts += $x.Value }
    if ($parts.Count -eq 0) { return $m.Line.Trim() }
    return ($parts -join ", ")
}

function Get-CargoSummary {
    param([string]$Log)
    $p = 0; $f = 0; $i = 0; $n = 0
    $lines = Select-String -Path $Log -Pattern '^test result: '
    foreach ($l in $lines) {
        $x = [regex]::Match($l.Line, '(\d+) passed; (\d+) failed; (\d+) ignored')
        if ($x.Success) {
            $p += [int]$x.Groups[1].Value; $f += [int]$x.Groups[2].Value; $i += [int]$x.Groups[3].Value; $n++
        }
    }
    if ($n -eq 0) { return "no cargo test result lines found" }
    return "$p passed, $f failed, $i ignored ($n test binaries)"
}

function Test-Pytest {
    param([string]$Name, [string]$Log, [string[]]$PytestArgs = @(), [switch]$SkipIsFailure)
    # A user's PYTEST_ADDOPTS (for example -l) could print local variables, including a password.
    Remove-Item -Path Env:\PYTEST_ADDOPTS -ErrorAction SilentlyContinue
    $all = @("-m", "pytest", "-q", "--tb=short") + $PytestArgs
    $code = Invoke-Native -Exe $VenvPy -ArgList $all -Log $Log
    $detail = Get-PytestSummary -Log $Log
    $onlySkipped = ($detail -match 'skipped') -and ($detail -notmatch 'passed')
    if ($code -ne 0) {
        Add-Result $Name "FAIL" ("exit $code; " + $detail)
    } elseif ($onlySkipped) {
        if ($SkipIsFailure) { Add-Result $Name "FAIL" ("nothing ran; " + $detail) } else { Add-Result $Name "SKIP" $detail }
    } else {
        Add-Result $Name "PASS" $detail
    }
}

function Test-Step {
    param([string]$Name, [string]$Exe, [string[]]$ArgList, [string]$Log, [bool]$CargoTest = $false)
    $code = Invoke-Native -Exe $Exe -ArgList $ArgList -Log $Log
    $detail = "exit $code"
    if ($CargoTest) { $detail = Get-CargoSummary -Log $Log }
    if ($code -eq 0) { Add-Result $Name "PASS" $detail } else { Add-Result $Name "FAIL" ("exit $code; " + $detail) }
    return ($code -eq 0)
}

function Get-OneLine {
    param([string]$Exe, [string[]]$ArgList)
    $old = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { return ((& $Exe @ArgList 2>&1 | Out-String).Trim()) } catch { return "unavailable" } finally { $ErrorActionPreference = $old }
}

# ---- Start ----
$RepoPath = (Resolve-Path -LiteralPath $RepoPath).Path
Set-Location -LiteralPath $RepoPath
$VenvPy = Join-Path $RepoPath ".venv\Scripts\python.exe"
$osText = [System.Environment]::OSVersion.VersionString
try { $osText = (Get-CimInstance Win32_OperatingSystem).Caption + " / " + $osText } catch { }
$psText = $PSVersionTable.PSVersion.ToString()
$pyText = Get-OneLine -Exe $PythonExe -ArgList @("--version")
Write-Host "Windows    : $osText"
Write-Host "PowerShell : $psText"
Write-Host "Python     : $pyText"
Write-Host "Repo       : $RepoPath"
Write-Host "Log dir    : $LogDir"

if (-not (Test-Path -LiteralPath (Join-Path $RepoPath "pyproject.toml"))) {
    Add-Result "Repository check" "FAIL" "pyproject.toml not found in RepoPath"
    $Abort = $true
}

# ---- Python ----
$setupLog = Join-Path $LogDir "python-setup.log"
if ($Abort) {
    Add-Result "Python setup" "NOT RUN" "repository check failed"
} else {
    $ok = $true
    if (-not (Test-Path -LiteralPath $VenvPy)) {
        $ok = Test-Step -Name "Python venv" -Exe $PythonExe -ArgList @("-m", "venv", (Join-Path $RepoPath ".venv")) -Log (Join-Path $LogDir "python-venv.log")
        if ($ok) { "venv created: see python-venv.log" | Out-File -FilePath $setupLog -Encoding utf8 }
    } else {
        "venv already exists, reused" | Out-File -FilePath $setupLog -Encoding utf8
        Add-Result "Python venv" "PASS" "already exists"
    }
    if ($ok) {
        $ok = Test-Step -Name "Python install (editable)" -Exe $VenvPy -ArgList @("-m", "pip", "install", "-e", ".[dev,serve,browser,windows]") -Log $setupLog
    }
    if (-not $ok) { $Abort = $true }
}
if ($Abort) {
    Add-Result "Python targeted tests" "NOT RUN" "setup failed"
    Add-Result "Python full suite" "NOT RUN" "setup failed"
} else {
    Test-Pytest -Name "Python targeted tests" -Log (Join-Path $LogDir "python-tests.log") -PytestArgs @("tests/test_serve_auth.py", "tests/test_browser_element_mapping.py")
    Test-Pytest -Name "Python full suite" -Log (Join-Path $LogDir "python-tests-full.log")
}

# ---- Rust ----
$NextExe = Join-Path $RepoPath "target\release\argus-next.exe"
$BenchExe = Join-Path $RepoPath "target\release\argus-bench.exe"
if ($SkipRust -or $Abort) {
    $why = "-SkipRust"; $st = "SKIP"
    if ($Abort) { $why = "earlier setup failure"; $st = "NOT RUN" }
    Add-Result "Rust tests" $st $why
    Add-Result "Rust release build" $st $why
    Add-Result "argus-next --version" $st $why
} else {
    [void](Test-Step -Name "Rust tests" -Exe "cargo" -ArgList @("test", "--workspace", "--locked") -Log (Join-Path $LogDir "rust-test.log") -CargoTest $true)
    $built = Test-Step -Name "Rust release build" -Exe "cargo" -ArgList @("build", "--release", "-p", "argus-cli", "--locked") -Log (Join-Path $LogDir "rust-build.log")
    if ($built -and (Test-Path -LiteralPath $NextExe)) {
        [void](Test-Step -Name "argus-next --version" -Exe $NextExe -ArgList @("--version") -Log (Join-Path $LogDir "rust-version.log"))
    } else {
        Add-Result "argus-next --version" "NOT RUN" "release build missing"
    }
}

# ---- Bench (command line from bench/README.md: argus-bench run --scenario ... --runs N --json --out ...) ----
function Invoke-BenchScenario {
    param([string]$Scenario)
    $jsonFile = Join-Path $LogDir ("bench-" + $Scenario + ".json")
    $outLog = Join-Path $LogDir ("bench-" + $Scenario + ".stdout.log")
    $errLog = Join-Path $LogDir ("bench-" + $Scenario + ".stderr.log")
    $toml = Join-Path $RepoPath ("bench\scenarios\" + $Scenario + ".toml")
    $a = "run --scenario " + (Q $toml) + " --runs 1 --json --out " + (Q $jsonFile) + " --python " + (Q $VenvPy) + " --root " + (Q $RepoPath)
    if (Test-Path -LiteralPath $NextExe) { $a += " --argus-next " + (Q $NextExe) }
    $code = $null
    try {
        $p = Start-Process -FilePath $BenchExe -ArgumentList $a -NoNewWindow -Wait -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog
        $code = $p.ExitCode
    } catch {
        ("ERROR: " + $_.Exception.Message) | Out-File -FilePath $errLog -Encoding utf8
    }
    $status = "unknown"
    if (Test-Path -LiteralPath $jsonFile) {
        try { $status = [string]((Get-Content -LiteralPath $jsonFile -Raw | ConvertFrom-Json).status) } catch { $status = "unreadable" }
    }
    $name = "Bench " + $Scenario
    $zero = ($null -ne $code) -and ($code -eq 0)
    if ($zero -and $status -eq "ok") {
        Add-Result $name "PASS" "status ok"
    } elseif ($zero -and ($status -eq "pending" -or $status -eq "skipped")) {
        Add-Result $name "SKIP" "status $status"
    } else {
        Add-Result $name "FAIL" ("exit " + $code + ", status " + $status + "; see " + $errLog)
    }
}

if ($SkipBench -or $Abort) {
    $st = "SKIP"; $why = "-SkipBench"
    if ($Abort) { $st = "NOT RUN"; $why = "earlier setup failure" }
    Add-Result "Bench" $st $why
} else {
    $benchReady = Test-Path -LiteralPath $BenchExe
    if (-not $benchReady) {
        # bench/README.md: cargo build --release -p argus-bench
        $benchReady = Test-Step -Name "Bench harness build" -Exe "cargo" -ArgList @("build", "--release", "-p", "argus-bench", "--locked") -Log (Join-Path $LogDir "rust-bench-build.log")
    }
    if ($benchReady) {
        foreach ($s in @("python-pass", "python-help", "argus-next-help")) { Invoke-BenchScenario -Scenario $s }
    } else {
        Add-Result "Bench scenarios" "NOT RUN" "argus-bench is not built"
    }
}

# ---- Native media (opt-in, elevated, dedicated non-admin test account) ----
if (-not $NativeMediaTest) {
    Add-Result "Native media test" "SKIP" "-NativeMediaTest not given"
} elseif ($Abort) {
    Add-Result "Native media test" "NOT RUN" "earlier setup failure"
} else {
    $identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object System.Security.Principal.WindowsPrincipal($identity)
    if (-not $principal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Write-Host "Native media test needs an elevated PowerShell (Run as administrator). Section skipped."
        Add-Result "Native media test" "NOT RUN" "session is not elevated"
    } else {
        $bstr = [IntPtr]::Zero
        $secure = $null
        $env:ARGUS_NATIVE_BOOTSTRAP_TEST = "1"
        try {
            $env:ARGUS_BOOTSTRAP_TEST_USER = Read-Host "Dedicated NON-ADMIN test account name"
            $secure = Read-Host "Password for that account (input is hidden)" -AsSecureString
            if ([string]::IsNullOrEmpty($env:ARGUS_BOOTSTRAP_TEST_USER) -or $secure.Length -eq 0) {
                Add-Result "Native media test" "NOT RUN" "account name or password was empty"
            } else {
                $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
                $env:ARGUS_BOOTSTRAP_TEST_PASSWORD = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
                [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
                $bstr = [IntPtr]::Zero
                Test-Pytest -Name "Native media test" -Log (Join-Path $LogDir "native-media.log") -PytestArgs @("tests/test_windows_bootstrap_media.py", "-k", "native_ntfs") -SkipIsFailure
            }
        } catch {
            Add-Result "Native media test" "FAIL" "section error (details withheld)"
        } finally {
            Remove-Item -Path Env:\ARGUS_BOOTSTRAP_TEST_PASSWORD -ErrorAction SilentlyContinue
            if ($bstr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
            if ($null -ne $secure) { $secure.Dispose() }
            Remove-Item -Path Env:\ARGUS_BOOTSTRAP_TEST_USER -ErrorAction SilentlyContinue
            Remove-Item -Path Env:\ARGUS_NATIVE_BOOTSTRAP_TEST -ErrorAction SilentlyContinue
        }
    }
}

# ---- Summary ----
$out = @("Argus PR 30 verification summary", "Date       : $Stamp", "Windows    : $osText", "PowerShell : $psText", "Python     : $pyText", "Repo       : $RepoPath", "")
foreach ($r in $Results) { $out += ("{0,-8} {1}: {2}" -f $r.Status, $r.Name, $r.Detail) }
$failCount = @($Results | Where-Object { $_.Status -eq "FAIL" }).Count
$out += ""
if ($failCount -eq 0) { $out += "OVERALL: PASS (every section that ran passed)" } else { $out += "OVERALL: FAIL ($failCount failing step(s))" }
$out += "Log directory: $LogDir"
Write-Host ""
$out | ForEach-Object { Write-Host $_ }
$out | Set-Content -LiteralPath (Join-Path $LogDir "summary.txt") -Encoding ASCII
if ($failCount -eq 0) { exit 0 } else { exit 1 }
