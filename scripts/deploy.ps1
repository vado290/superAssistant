<#
Deploy the current assistant-kernel source: build one wheel, verify it in the
`test` venv, then install the same wheel into the `prod` venv and restart.
Forward-only promotion: back up before mutation; failures retain artifacts for repair.
Never reinstall an old wheel against a migrated database.

  .\scripts\deploy.ps1                 # full deploy
  .\scripts\deploy.ps1 -SkipTests      # skip the test-venv pytest step
  .\scripts\deploy.ps1 -TestOnly       # build + test venv only, prod untouched
#>
param(
    [string]$TestPath = "tests",
    [switch]$SkipTests,
    [switch]$TestOnly
)

$ErrorActionPreference = "Stop"
$Kernel = "/home/v/AI/assistant-kernel"
$Runtime = "/home/v/AI/assistant-data/runtime"
$Venvs = "$Runtime/kernel-venvs"
$Wheels = "$Runtime/wheels"
$BasePython = "/home/v/miniconda3/bin/python"
$SupervisorConf = "/etc/supervisor/conf.d/assistant.conf"
$HealthUrl = "http://127.0.0.1:18001"

# Progress goes to the host stream: PowerShell functions return everything written to output.
function Step($text) { Write-Host "== $text" }
function InContainer {
    # Arguments are passed as separate tokens; never embed double quotes (PowerShell 5.1 strips them).
    docker exec AI /usr/bin/env -u PYTHONPATH -u PYTHONHOME @args | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "container command failed: $($args -join ' ')" }
}
function Supervisor { docker exec AI supervisorctl -c $SupervisorConf @args }
function SupervisorDo {
    Supervisor @args | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "supervisor command failed: $($args -join ' ')" }
}

function Ensure-Venv($name) {
    $python = "$Venvs/$name/bin/python"
    docker exec AI test -x $python
    if ($LASTEXITCODE -ne 0) { InContainer $BasePython -m venv "$Venvs/$name" }
    InContainer $python -m pip install --quiet --upgrade pip
    InContainer $python -m pip install --quiet -r "$Kernel/requirements/kernel.lock"
    return $python
}

function Install-Wheel($python, $wheel) {
    InContainer $python -m pip install --quiet --no-deps --force-reinstall $wheel
    InContainer $python -m pip check
}

function Kernel-Programs {
    $lines = @(Supervisor status)
    if ($LASTEXITCODE -notin @(0, 3)) { throw "cannot read Supervisor status" }
    $names = $lines | ForEach-Object { ($_ -split '\s+')[0] } | Where-Object { $_ -like 'kernel-*' }
    if (-not $names) { throw "no Kernel programs registered; refusing deployment" }
    $names | ForEach-Object { if ($_ -like '*:*') { ($_ -split ':')[0] + ':*' } else { $_ } } | Select-Object -Unique
}

function Wait-Ready($expectedVersion) {
    # Ready = every kernel-* program RUNNING (supervisord counts that only after startsecs) and the
    # API reports the deployed version. The legacy /health/ready judged the retired workers, so it is not used.
    for ($i = 0; $i -lt 45; $i++) {
        try {
            $lines = @(Supervisor status | Where-Object { $_ -like 'kernel-*' })
            $running = $lines.Count -gt 0 -and -not ($lines | Where-Object { $_ -notmatch '\sRUNNING\s' })
            $version = Invoke-RestMethod -Uri "$HealthUrl/version" -TimeoutSec 5
            if ($running -and $version.version -eq $expectedVersion) { return $true }
        } catch {}
        Start-Sleep -Seconds 2
    }
    return $false
}


# 0. production runs only committed code (system map analysis 2026-10-22, K2): the wheel is built from the
#    working copy, so uncommitted or untracked files would reach prod without any record. -TestOnly may run anyway.
if (-not $TestOnly) {
    $repo = Join-Path $PSScriptRoot ".."
    $dirty = @(git -C $repo status --porcelain --untracked-files=no)
    if ($LASTEXITCODE -ne 0) { throw "git status failed; cannot confirm the source is committed" }
    $untracked = @(git -C $repo ls-files --others --exclude-standard -- src tests scripts deploy requirements pyproject.toml LICENSE)
    if ($LASTEXITCODE -ne 0) { throw "cannot check untracked release inputs" }
    $dirty += $untracked
    if ($dirty) {
        Write-Host $dirty
        throw "the working copy has uncommitted changes: commit them first (or use -TestOnly to only run the tests)"
    }
}

# 1. build one wheel from the checked-out source
$version = (Select-String -LiteralPath (Join-Path $PSScriptRoot "..\pyproject.toml") -Pattern '^version = "(.+)"$').Matches[0].Groups[1].Value
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
Step "build wheel $version ($stamp)"
# setuptools reuses build/ between builds, so files deleted from the source would ship again.
InContainer rm -rf "$Kernel/build"
InContainer $BasePython -m pip wheel --quiet --no-deps -w "$Wheels/$stamp" $Kernel
$wheel = "$(docker exec AI sh -c "ls $Wheels/$stamp/*.whl")".Trim()
if (-not $wheel) { throw "wheel build produced no file" }

# 2. verify in the test venv
Step "install into test venv"
$testPython = Ensure-Venv "test"
InContainer $testPython -m pip install --quiet -r "$Kernel/requirements/test.lock"
Install-Wheel $testPython $wheel
if (-not $SkipTests) {
    Step "pytest $TestPath (temporary databases)"
    InContainer sh -c "cd $Kernel && $testPython -m pytest $TestPath -q -p no:cacheprovider"
}
if ($TestOnly) {
    docker exec AI rm -rf "$Wheels/$stamp"
    Write-Output "TEST_ONLY_OK; version=$version (test venv only; wheel discarded)"
    return
}

# 3. preflight before any production mutation. Backups are kept outside disposable wheel directories.
Step "check migration ledger and backup tools"
InContainer $testPython -m assistant_kernel.core.migrate --dry-run
InContainer $testPython -m assistant_kernel.core.migrate --check-backup-tools
$recovery = "$Runtime/release-backups/$stamp"
InContainer mkdir -m 0700 -p $recovery
InContainer cp -p $SupervisorConf "$recovery/assistant.conf"
$programs = @(Kernel-Programs)
$productionChanged = $false
try {
    Step "stop: $($programs -join ' ') (running turns drain first)"
    SupervisorDo stop @programs
    Step "back up database before migration"
    InContainer $testPython -m assistant_kernel.core.migrate --backup "$recovery/database.dump"
    # From here even a failed dependency install may have changed prod. Do not guess compatibility.
    $productionChanged = $true
    $prodPython = Ensure-Venv "prod"
    Step "install into prod venv"
    Install-Wheel $prodPython $wheel
    Step "migrate (forward only)"
    InContainer $prodPython -m assistant_kernel.core.migrate
    Step "install supervisor config and start"
    InContainer install -m 0644 "$Kernel/deploy/supervisord.conf" $SupervisorConf
    SupervisorDo reread
    SupervisorDo update
    $programs = @(Kernel-Programs)
    SupervisorDo restart @programs
    if (-not (Wait-Ready $version)) { throw "health check failed for $version" }
}
catch {
    $failure = $_
    if (-not $productionChanged) {
        SupervisorDo start @programs
        throw "deployment aborted before production installation/migration; original services restarted: $failure"
    }
    # A migration may have committed before a later migration failed. Never install an old wheel here.
    try { SupervisorDo stop @programs }
    catch { Write-Warning "could not stop all Kernel programs; inspect Supervisor before repair: $_" }
    throw "FORWARD_REPAIR_REQUIRED; recovery=$recovery; wheel=$wheel; no automatic restore or downgrade: $failure"
}

# 4. mark success. Keep recovery artifacts and failed-release wheels for explicit operator review.
InContainer touch "$Wheels/$stamp/.deployed"
Write-Output "ASSISTANT_KERNEL_DEPLOYED; version=$version; wheel=$wheel; recovery=$recovery"
