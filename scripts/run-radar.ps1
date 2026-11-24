param(
    [ValidateSet('status', 'plan', 'search', 'resume', 'details', 'report')]
    [string]$Action = 'status',

    [string]$RunId = '',
    [string]$RunDate = '',
    [string]$PlanPath = '',
    [int]$OperationLimit = 0,
    [int]$DelaySeconds = 10,
    [int]$MinCollected = 1000,
    [int]$DetailTarget = 20,
    [int]$DetailBatchSize = 0,
    [switch]$ResumeDetails,
    [string]$EngineRoot = '',
    [string]$PythonPath = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0
if ($ResumeDetails -and $Action -ne 'details') {
    throw '-ResumeDetails is only valid with -Action details.'
}

function Resolve-ExistingPath {
    param(
        [Parameter(Mandatory)][string]$LiteralPath,
        [Parameter(Mandatory)][string]$Label
    )
    if (-not (Test-Path -LiteralPath $LiteralPath)) {
        throw "$Label is missing: $LiteralPath"
    }
    return (Resolve-Path -LiteralPath $LiteralPath).Path
}

function Invoke-Checked {
    param(
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string[]]$Arguments,
        [Parameter(Mandatory)][string]$Label
    )
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed with exit code $LASTEXITCODE"
    }
}

function Invoke-Collector {
    param([Parameter(Mandatory)][string[]]$Arguments)
    & powershell.exe @Arguments
    if ($LASTEXITCODE -notin @(0, 2)) {
        throw "Collector failed with exit code $LASTEXITCODE"
    }
    # Exit 2 is a checkpointed failure. The persisted status remains authoritative.
}

function New-RadarPlan {
    param(
        [Parameter(Mandatory)][string]$Destination,
        [Parameter(Mandatory)][string]$Date
    )
    $Destination = [IO.Path]::GetFullPath($Destination)
    $parent = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    Invoke-Checked -Executable $script:Python -Label 'Radar plan generation' -Arguments @(
        $script:RadarCli,
        'plan',
        '--config', $script:ConfigPath,
        '--run-date', $Date,
        '--plan', $Destination
    ) | Out-Host
    return (Resolve-Path -LiteralPath $Destination).Path
}

if ([string]::IsNullOrWhiteSpace($EngineRoot)) { $EngineRoot = $env:XHS_RADAR_ENGINE_ROOT }
if ([string]::IsNullOrWhiteSpace($EngineRoot)) {
    $EngineRoot = [Environment]::GetEnvironmentVariable(
        'XHS_RADAR_ENGINE_ROOT',
        'User'
    )
}
if ([string]::IsNullOrWhiteSpace($EngineRoot)) {
    $engineFolder = -join @(
        [char]0x8003,
        [char]0x7814,
        [char]0x81EA,
        [char]0x52A8,
        [char]0x5316,
        [char]0x5BA3,
        [char]0x4F20
    )
    $EngineRoot = Join-Path `
        ([Environment]::GetFolderPath('MyDocuments')) `
        $engineFolder
}
$CodeRoot = Split-Path -Parent $PSScriptRoot
if ($Action -ne 'status') {
    $EngineRoot = Resolve-ExistingPath -LiteralPath $EngineRoot -Label 'Central radar engine root'
}
if ([IO.Path]::GetFullPath($EngineRoot).TrimEnd('\') -eq $CodeRoot.TrimEnd('\')) {
    throw 'Use the external device root for shared state.'
}
$env:XHS_RADAR_ENGINE_ROOT = $EngineRoot

$RadarCli = Resolve-ExistingPath `
    -LiteralPath (Join-Path $CodeRoot 'scripts\xhs-radar\xhs_radar.py') `
    -Label 'Radar CLI'
$Collector = Resolve-ExistingPath `
    -LiteralPath (Join-Path $CodeRoot 'scripts\xhs-radar\collect-radar-run.ps1') `
    -Label 'Read-only collector'
$DetailBuilder = Resolve-ExistingPath `
    -LiteralPath (Join-Path $CodeRoot 'scripts\xhs-radar\build-detail-candidates.py') `
    -Label 'Detail candidate builder'
$DetailCollector = Resolve-ExistingPath `
    -LiteralPath (Join-Path $CodeRoot 'scripts\xhs-radar\collect-qualified-details.ps1') `
    -Label 'Qualified detail collector'
$ConfigPath = Join-Path $EngineRoot 'config\xhs-radar-keywords.json'
if ($Action -ne 'status') {
    $ConfigPath = Resolve-ExistingPath -LiteralPath $ConfigPath -Label 'Shared keyword configuration'
}
if ([string]::IsNullOrWhiteSpace($PythonPath)) { $PythonPath = $env:XHS_RADAR_PYTHON }
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $PythonPath = [Environment]::GetEnvironmentVariable('XHS_RADAR_PYTHON', 'User')
}
if ([string]::IsNullOrWhiteSpace($PythonPath)) {
    $bundledPython = Join-Path $env:USERPROFILE `
        '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
    if (Test-Path -LiteralPath $bundledPython) {
        $PythonPath = $bundledPython
    }
    else {
        $command = Get-Command python -ErrorAction SilentlyContinue
        if ($null -eq $command) {
            throw 'Python was not found. Set the user environment variable XHS_RADAR_PYTHON.'
        }
        $PythonPath = $command.Source
    }
}
$Python = Resolve-ExistingPath -LiteralPath $PythonPath -Label 'Python runtime'

$RunsRoot = Join-Path $EngineRoot 'data\xhs-radar\runs'
$PlansRoot = Join-Path $EngineRoot 'data\xhs-radar\plans'
$ReportsRoot = Join-Path $EngineRoot 'reports\xhs-radar'
$DatabasePath = Join-Path $EngineRoot 'data\xhs-radar\xhs-radar.sqlite3'

if ([string]::IsNullOrWhiteSpace($RunDate)) {
    $RunDate = Get-Date -Format 'yyyy-MM-dd'
}
if ([string]::IsNullOrWhiteSpace($RunId) -and $Action -eq 'search') {
    $RunId = ('radar-' + (Get-Date -Format "yyyyMMdd't'HHmmss")).ToLowerInvariant()
}

$deviceBinding = Join-Path $CodeRoot 'scripts\xhs-radar\readonly-provider-version.ps1'
$ToolRoot = Join-Path $EngineRoot '.local-tools\xiaohongshu-mcp'
. $deviceBinding
$providerPresent = Test-Path -LiteralPath $MainExe -PathType Leaf
$historyPresent = Test-Path -LiteralPath $DatabasePath -PathType Leaf
$configPresent = Test-Path -LiteralPath $ConfigPath -PathType Leaf
$ready = $providerPresent -and $historyPresent -and $configPresent
if ($Action -in @('search', 'resume', 'details', 'report') -and -not $historyPresent) {
    throw 'The shared device database must already exist.'
}
if ($Action -eq 'status') {
    $latestRun = Get-ChildItem -LiteralPath $RunsRoot -Directory -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    $latestManifest = $null
    if ($null -ne $latestRun) {
        $manifestPath = Join-Path $latestRun.FullName 'manifest.json'
        if (Test-Path -LiteralPath $manifestPath) {
            $latestManifest = Get-Content -Encoding UTF8 -Raw -LiteralPath $manifestPath |
                ConvertFrom-Json
        }
    }
    [pscustomobject]@{
        status = if ($ready) { 'ready' } else { 'device_incomplete' }
        action = 'status'
        engine_ready = $ready
        keyword_config_present = $configPresent
        provider_present = $providerPresent
        authenticated = $null
        authentication_state = 'unknown'
        authentication_checked_at = $null
        engine_root = $EngineRoot
        central_database_present = (Test-Path -LiteralPath $DatabasePath)
        latest_run_id = if ($null -ne $latestManifest) { $latestManifest.run_id } else { $null }
        latest_run_status = if ($null -ne $latestManifest) { $latestManifest.status } else { $null }
        network_request_made = $false
        mutation_tools_allowed = $false
    } | ConvertTo-Json -Depth 5
    exit 0
}

if (-not [string]::IsNullOrWhiteSpace($RunId) -and
    $RunId -notmatch '^[a-z0-9][a-z0-9-]{5,80}$') {
    throw 'RunId must contain only lowercase letters, digits, and hyphens.'
}

if ($Action -eq 'plan') {
    if ([string]::IsNullOrWhiteSpace($RunId)) {
        $RunId = ('radar-plan-' + (Get-Date -Format "yyyyMMdd't'HHmmss")).ToLowerInvariant()
    }
    if ([string]::IsNullOrWhiteSpace($PlanPath)) {
        $PlanPath = Join-Path $PlansRoot "$RunId-plan.json"
    }
    $resolvedPlan = New-RadarPlan -Destination $PlanPath -Date $RunDate
    [pscustomobject]@{
        status = 'completed'
        action = 'plan'
        run_id = $RunId
        plan_path = $resolvedPlan
        network_request_made = $false
    } | ConvertTo-Json -Depth 5
    exit 0
}

if ($Action -in @('resume', 'details', 'report') -and
    [string]::IsNullOrWhiteSpace($RunId)) {
    throw "Action '$Action' requires -RunId."
}


$RunRoot = Join-Path $RunsRoot $RunId
$ManifestPath = Join-Path $RunRoot 'manifest.json'
$ReportPath = Join-Path $ReportsRoot "$RunId-radar.md"
$ActionStatus = 'completed'
$DetailSummary = $null

if ($Action -eq 'search') {
    if (Test-Path -LiteralPath $RunRoot) {
        throw "Run already exists; use -Action resume or a new RunId: $RunRoot"
    }
    if ([string]::IsNullOrWhiteSpace($PlanPath)) {
        $PlanPath = Join-Path $PlansRoot "$RunId-plan.json"
        $PlanPath = New-RadarPlan -Destination $PlanPath -Date $RunDate
    }
    else {
        $PlanPath = Resolve-ExistingPath -LiteralPath $PlanPath -Label 'Immutable radar plan'
    }
    $collectorArgs = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', $Collector,
        '-RunId', $RunId,
        '-PlanPath', $PlanPath,
        '-OperationLimit', [string]$OperationLimit,
        '-DelaySeconds', [string]$DelaySeconds
    )
    Invoke-Collector -Arguments $collectorArgs
}

if ($Action -eq 'resume') {
    $ManifestPath = Resolve-ExistingPath -LiteralPath $ManifestPath -Label 'Run manifest'
    if ([string]::IsNullOrWhiteSpace($PlanPath)) {
        $manifest = Get-Content -Encoding UTF8 -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
        $PlanPath = [string]$manifest.plan_path
    }
    $PlanPath = Resolve-ExistingPath -LiteralPath $PlanPath -Label 'Original immutable radar plan'
    $collectorArgs = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', $Collector,
        '-RunId', $RunId,
        '-PlanPath', $PlanPath,
        '-OperationLimit', [string]$OperationLimit,
        '-DelaySeconds', [string]$DelaySeconds,
        '-Resume'
    )
    Invoke-Collector -Arguments $collectorArgs
}

if ($Action -in @('search', 'resume')) {
    $RunRoot = Resolve-ExistingPath -LiteralPath $RunRoot -Label 'Completed radar run'
    New-Item -ItemType Directory -Force -Path $ReportsRoot | Out-Null
    $searchSummary = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $ActionStatus = [string]$searchSummary.status
    if ($ActionStatus -eq 'completed') {
        Invoke-Checked -Executable $Python -Label 'Radar run import' -Arguments @(
            $RadarCli, 'import-run', '--db', $DatabasePath, '--config', $ConfigPath, '--run-dir', $RunRoot
        )
    }
    Invoke-Checked -Executable $Python -Label 'Radar run report' -Arguments @(
        $RadarCli, 'report-run', '--db', $DatabasePath, '--config', $ConfigPath, '--run-dir', $RunRoot, '--report', $ReportPath
    )
}

if ($Action -eq 'details') {
    $RunRoot = Resolve-ExistingPath -LiteralPath $RunRoot -Label 'Completed radar run'
    $searchSummary = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ([string]$searchSummary.status -ne 'completed') {
        throw 'Search run must be completed before collecting details.'
    }
    $selectionPath = Join-Path $RunRoot 'detail-selection-v2.json'
    $detailSummaryPath = Join-Path $RunRoot 'qualified-details-v2-summary.json'
    $detailsPath = Join-Path $RunRoot 'qualified-details-v2.jsonl'
    $hasDetailOutput = (Test-Path -LiteralPath $detailSummaryPath) -or (Test-Path -LiteralPath $detailsPath)
    if ($hasDetailOutput -and -not $ResumeDetails) {
        throw 'Detail output exists; use -Action details -ResumeDetails with the same RunId.'
    }
    if ($ResumeDetails -and -not (Test-Path -LiteralPath $selectionPath)) {
        throw 'Original detail selection is missing; refusing to rebuild it during resume.'
    }
    if (-not (Test-Path -LiteralPath $selectionPath)) {
        Invoke-Checked -Executable $Python -Label 'Strict detail candidate selection' -Arguments @(
            $DetailBuilder,
            '--run-dir', $RunRoot,
            '--config', $ConfigPath,
            '--target', [string]$DetailTarget,
            '--min-collected', [string]$MinCollected,
            '--max-per-author', '1'
        )
    }
    $detailArgs = @(
        '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', $DetailCollector,
        '-RunId', $RunId,
        '-BatchSize', [string]$DetailBatchSize,
        '-DelaySeconds', [string]$DelaySeconds
    )
    if ($ResumeDetails) { $detailArgs += '-Resume' }
    Invoke-Collector -Arguments $detailArgs
    $DetailSummary = Get-Content -LiteralPath $detailSummaryPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $ActionStatus = [string]$DetailSummary.status
    if ($ActionStatus -eq 'completed') {
        Invoke-Checked -Executable $Python -Label 'Qualified detail import' -Arguments @(
            $RadarCli, 'import-details', '--db', $DatabasePath, '--run-dir', $RunRoot
        )
    }
    New-Item -ItemType Directory -Force -Path $ReportsRoot | Out-Null
    Invoke-Checked -Executable $Python -Label 'Radar detail report' -Arguments @(
        $RadarCli, 'report-run', '--db', $DatabasePath, '--config', $ConfigPath, '--run-dir', $RunRoot, '--report', $ReportPath
    )
}

if ($Action -eq 'report') {
    $RunRoot = Resolve-ExistingPath -LiteralPath $RunRoot -Label 'Existing radar run'
    New-Item -ItemType Directory -Force -Path $ReportsRoot | Out-Null
    Invoke-Checked -Executable $Python -Label 'Existing radar report' -Arguments @(
        $RadarCli, 'report-run', '--db', $DatabasePath, '--config', $ConfigPath, '--run-dir', $RunRoot, '--report', $ReportPath
    )
}

$manifest = $null
if (Test-Path -LiteralPath $ManifestPath) {
    $manifest = Get-Content -Encoding UTF8 -Raw -LiteralPath $ManifestPath | ConvertFrom-Json
}
[pscustomobject]@{
    status = $ActionStatus
    action = $Action
    run_id = $RunId
    run_status = if ($null -ne $manifest) { $manifest.status } else { $null }
    counts = if ($null -ne $manifest) { $manifest.counts } else { $null }
    detail_status = if ($null -ne $DetailSummary) { $DetailSummary.status } else { $null }
    detail_counts = if ($null -ne $DetailSummary) { $DetailSummary.counts } else { $null }
    stop_reason = if ($null -ne $DetailSummary) { $DetailSummary.stop_reason } elseif ($null -ne $manifest -and $manifest.PSObject.Properties.Name -contains 'stop_reason') { $manifest.stop_reason } else { $null }
    run_directory = $RunRoot
    report_path = if (Test-Path -LiteralPath $ReportPath) { $ReportPath } else { $null }
    central_database = $DatabasePath
    mutation_tools_allowed = $false
} | ConvertTo-Json -Depth 8
if ($ActionStatus -eq 'failed') { exit 2 }
