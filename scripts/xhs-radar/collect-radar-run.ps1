param(
    [Parameter(Mandatory)]
    [string]$RunId,

    [string]$PlanPath = '',
    [int]$OperationLimit = 0,
    [int]$DelaySeconds = 6,
    [switch]$VisibleBrowser,
    [switch]$Resume
)

$ErrorActionPreference = 'Stop'

. (Join-Path $PSScriptRoot '..\xhs-local\common.ps1')
. (Join-Path $PSScriptRoot 'readonly-provider-version.ps1')
. (Join-Path $PSScriptRoot '..\xhs-local\port-guard.ps1')
. (Join-Path $PSScriptRoot '..\xhs-local\case-sensitive-json.ps1')
. (Join-Path $PSScriptRoot 'session-lifecycle.ps1')

Assert-VerifiedExecutable `
    -LiteralPath $MainExe `
    -ExpectedSha256 $ExpectedMainSha256

Initialize-IsolatedRuntime

function ConvertFrom-CodePoints {
    param([Parameter(Mandatory)][int[]]$CodePoints)
    return -join @($CodePoints | ForEach-Object { [char]$_ })
}

$TextSortComprehensive = ConvertFrom-CodePoints @(0x7EFC, 0x5408)
$TextSortNewest = ConvertFrom-CodePoints @(0x6700, 0x65B0)
$TextSortMostLiked = ConvertFrom-CodePoints @(0x6700, 0x591A, 0x70B9, 0x8D5E)
$TextSortMostCommented = ConvertFrom-CodePoints @(0x6700, 0x591A, 0x8BC4, 0x8BBA)
$TextSortMostCollected = ConvertFrom-CodePoints @(0x6700, 0x591A, 0x6536, 0x85CF)
$TextTimeUnlimited = ConvertFrom-CodePoints @(0x4E0D, 0x9650)
$TextTimeDay = ConvertFrom-CodePoints @(0x4E00, 0x5929, 0x5185)
$TextTimeWeek = ConvertFrom-CodePoints @(0x4E00, 0x5468, 0x5185)
$TextTimeHalfYear = ConvertFrom-CodePoints @(0x534A, 0x5E74, 0x5185)
$TextImageNote = ConvertFrom-CodePoints @(0x56FE, 0x6587)
$TextLoggedIn = ConvertFrom-CodePoints @(0x5DF2, 0x767B, 0x5F55)
$TextFilterUnavailable = ConvertFrom-CodePoints @(0x672A, 0x7B5B, 0x9009, 0xFF08, 0x7B5B, 0x9009, 0x9762, 0x677F, 0x4E0D, 0x53EF, 0x7528, 0xFF09)

if ($RunId -notmatch '^[a-z0-9][a-z0-9-]{5,80}$') {
    throw 'RunId must contain only lowercase letters, digits, and hyphens.'
}

if ([string]::IsNullOrWhiteSpace($PlanPath)) {
    $PlanPath = Join-Path $WorkspaceRoot `
        'data\xhs-radar\plans\2026-08-01-weekly-plan.json'
}
$PlanPath = (Resolve-Path -LiteralPath $PlanPath).Path
$PlanHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $PlanPath).Hash

$RunRoot = Join-Path $WorkspaceRoot "data\xhs-radar\runs\$RunId"
$ManifestPath = Join-Path $RunRoot 'manifest.json'
$ObservationsJsonl = Join-Path $RunRoot 'observations.jsonl'
$ObservationsCsv = Join-Path $RunRoot 'observations.csv'
$CacheRoot = Join-Path $RuntimeRoot 'research-cache\radar-runs'
$LookupPath = Join-Path $CacheRoot "$RunId-lookup.json"

if ((Test-Path -LiteralPath $RunRoot) -and -not $Resume) {
    throw "Run directory already exists; use -Resume or a new RunId: $RunRoot"
}
if ($Resume -and -not (Test-Path -LiteralPath $ManifestPath)) {
    throw "Resume requested but manifest is missing: $ManifestPath"
}

New-Item -ItemType Directory -Force -Path `
    $RunRoot,
    $CacheRoot,
    $RuntimeLogs | Out-Null

function Convert-MetricToNumber {
    param([AllowEmptyString()][string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) { return [long]0 }
    $text = $Value.Trim().Replace(',', '').Replace('+', '')
    $multiplier = 1.0
    $wan = [string][char]0x4E07
    $yi = [string][char]0x4EBF
    if ($text.EndsWith($wan)) {
        $multiplier = 10000.0
        $text = $text.Substring(0, $text.Length - 1)
    }
    elseif ($text.EndsWith($yi)) {
        $multiplier = 100000000.0
        $text = $text.Substring(0, $text.Length - 1)
    }
    $number = 0.0
    if ([double]::TryParse(
        $text,
        [System.Globalization.NumberStyles]::Float,
        [System.Globalization.CultureInfo]::InvariantCulture,
        [ref]$number
    )) {
        return [long][math]::Round($number * $multiplier)
    }
    return [long]0
}

function Get-Utf8ResponseContent {
    param([Parameter(Mandatory)]$Response)
    if ($Response.RawContentStream -and
        $Response.RawContentStream.PSObject.Methods.Name -contains 'ToArray') {
        return [System.Text.Encoding]::UTF8.GetString(
            $Response.RawContentStream.ToArray()
        )
    }
    return [string]$Response.Content
}

function ConvertFrom-McpHttpResponse {
    param([Parameter(Mandatory)]$Response)

    $content = Get-Utf8ResponseContent -Response $Response
    if ([string]::IsNullOrWhiteSpace($content)) { return $null }
    try {
        return $content | ConvertFrom-Json
    }
    catch {
        $dataLines = @(
            $content -split "`r?`n" |
                Where-Object { $_ -match '^data:\s*(.+)$' } |
                ForEach-Object { $Matches[1] } |
                Where-Object { $_ -ne '[DONE]' }
        )
        if ($dataLines.Count -eq 0) {
            throw 'MCP response was neither JSON nor an SSE data event.'
        }
        return $dataLines[-1] | ConvertFrom-Json
    }
}

function Invoke-McpPost {
    param(
        [Parameter(Mandatory)][hashtable]$Payload,
        [string]$SessionId
    )

    $headers = @{ Accept = 'application/json, text/event-stream' }
    if (-not [string]::IsNullOrWhiteSpace($SessionId)) {
        $headers['Mcp-Session-Id'] = $SessionId
    }
    Invoke-WebRequest `
        -UseBasicParsing `
        -Uri 'http://127.0.0.1:18060/mcp' `
        -Method Post `
        -ContentType 'application/json; charset=utf-8' `
        -Headers $headers `
        -Body ($Payload | ConvertTo-Json -Compress -Depth 30) `
        -TimeoutSec 100
}

function Stop-IsolatedBrowserProcesses {
    $isolatedPrefix = $RuntimeLocalAppData.ToLowerInvariant()
    Get-CimInstance Win32_Process `
        -Filter "Name = 'chrome.exe'" `
        -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ExecutablePath -and
            $_.ExecutablePath.ToLowerInvariant().StartsWith($isolatedPrefix)
        } |
        ForEach-Object {
            Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        }
}

function Normalize-ProcessPathVariable {
    # Some Codex-launched Windows processes contain both Path and PATH.
    # Windows PowerShell 5 Start-Process rejects that duplicate dictionary.
    # Normalize this process only; user and machine environment remain unchanged.
    $pathValue = [string]$env:Path
    [Environment]::SetEnvironmentVariable('PATH', $null, 'Process')
    [Environment]::SetEnvironmentVariable('Path', $null, 'Process')
    [Environment]::SetEnvironmentVariable('Path', $pathValue, 'Process')
}

function Get-ToolText {
    param([Parameter(Mandatory)]$ToolResult)
    return @(
        $ToolResult.result.content |
            Where-Object { $_.type -eq 'text' } |
            ForEach-Object { $_.text }
    ) -join "`n"
}

$AllowedToolCalls = @('check_login_status', 'search_feeds')

function Invoke-ReadOnlyTool {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][hashtable]$Arguments,
        [Parameter(Mandatory)][int]$Id,
        [Parameter(Mandatory)][string]$SessionId
    )

    if ($Name -notin $AllowedToolCalls) {
        throw "Refusing non-approved tool call: $Name"
    }
    $http = Invoke-McpPost -Payload @{
        jsonrpc = '2.0'
        id = $Id
        method = 'tools/call'
        params = @{
            name = $Name
            arguments = $Arguments
        }
    } -SessionId $SessionId
    $result = ConvertFrom-McpHttpResponse -Response $http
    if ($result.PSObject.Properties.Name -contains 'error' -and
        $result.error) {
        throw "$Name returned an MCP error: $($result.error | ConvertTo-Json -Compress)"
    }
    if ($result.result.PSObject.Properties.Name -contains 'isError' -and
        $result.result.isError) {
        throw "$Name failed: $(Get-ToolText -ToolResult $result)"
    }
    if ($Name -ne 'check_login_status') {
        Assert-RadarBusinessResponse -Text (Get-ToolText -ToolResult $result)
    }
    return $result
}

function Start-ReadOnlyMcpSession {
    param(
        [Parameter(Mandatory)][string]$StdoutPath,
        [Parameter(Mandatory)][string]$StderrPath
    )

    $process = $null
    try {
        Assert-LocalTcpPortFree -Port 18060
        Normalize-ProcessPathVariable
        $headlessArgument = if ($VisibleBrowser) {
            '-headless=false'
        } else {
            '-headless=true'
        }
        $process = Start-Process `
            -FilePath $MainExe `
            -ArgumentList @($headlessArgument, '-port=127.0.0.1:18060') `
            -WorkingDirectory $RuntimeRoot `
            -WindowStyle Hidden `
            -RedirectStandardOutput $StdoutPath `
            -RedirectStandardError $StderrPath `
            -PassThru

        $listening = $false
        foreach ($attempt in 1..60) {
            if ($process.HasExited) { break }
            if (Test-LocalTcpPort -Port 18060) {
                $listening = $true
                break
            }
            Start-Sleep -Milliseconds 500
            $process.Refresh()
        }
        if (-not $listening) {
            throw 'MCP server did not listen on 127.0.0.1:18060.'
        }

        $initializeHttp = Invoke-McpPost -Payload @{
            jsonrpc = '2.0'
            id = 1
            method = 'initialize'
            params = @{
                protocolVersion = '2025-03-26'
                capabilities = @{}
                clientInfo = @{
                    name = 'codex-xhs-radar-collector'
                    version = '1.0.0'
                }
            }
        }
        $initialize = ConvertFrom-McpHttpResponse -Response $initializeHttp
        $sessionId = [string]$initializeHttp.Headers['Mcp-Session-Id']
        if ($initialize.PSObject.Properties.Name -contains 'error' -and
            $initialize.error) {
            throw "MCP initialize failed: $($initialize.error | ConvertTo-Json -Compress)"
        }
        $null = Invoke-McpPost -Payload @{
            jsonrpc = '2.0'
            method = 'notifications/initialized'
            params = @{}
        } -SessionId $sessionId

        $loginResult = Invoke-ReadOnlyTool `
            -Name 'check_login_status' `
            -Arguments @{} `
            -Id 2 `
            -SessionId $sessionId
        $loginText = Get-ToolText -ToolResult $loginResult
        if ($loginText -notmatch [regex]::Escape($TextLoggedIn)) {
            Assert-RadarBusinessResponse -Text $loginText
            throw "Login validation did not confirm an authenticated session: $loginText"
        }

        return [ordered]@{
            Process = $process
            SessionId = $sessionId
        }
    }
    catch {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
            $process.WaitForExit(3000) | Out-Null
            if (-not $process.HasExited) {
                Stop-Process `
                    -Id $process.Id `
                    -Force `
                    -ErrorAction SilentlyContinue
            }
        }
        Stop-IsolatedBrowserProcesses
        throw
    }
}

function Stop-ReadOnlyMcpSession {
    param($Session)

    if ($Session -and $Session.Process -and -not $Session.Process.HasExited) {
        Stop-Process -Id $Session.Process.Id -ErrorAction SilentlyContinue
        $Session.Process.WaitForExit(3000) | Out-Null
        if (-not $Session.Process.HasExited) {
            Stop-Process `
                -Id $Session.Process.Id `
                -Force `
                -ErrorAction SilentlyContinue
        }
    }
    Stop-IsolatedBrowserProcesses
    Start-Sleep -Milliseconds 750
}

$plan = Get-Content -LiteralPath $PlanPath -Raw -Encoding UTF8 |
    ConvertFrom-Json
if ([string]$plan.schema_version -ne '1.0') {
    throw "Unsupported plan schema: $($plan.schema_version)"
}
$operations = @($plan.operations)
if ($OperationLimit -gt 0) {
    $operations = @($operations | Select-Object -First $OperationLimit)
}
if ($operations.Count -eq 0) {
    throw 'Plan contains no selected operations.'
}

$allowedSort = @(
    $TextSortComprehensive,
    $TextSortNewest,
    $TextSortMostLiked,
    $TextSortMostCommented,
    $TextSortMostCollected
)
$allowedTime = @(
    $TextTimeUnlimited,
    $TextTimeDay,
    $TextTimeWeek,
    $TextTimeHalfYear
)
foreach ($operation in $operations) {
    if ([string]$operation.sort_mode -notin $allowedSort) {
        throw "Unsupported sort mode in operation $($operation.operation)"
    }
    if ([string]$operation.time_filter -notin $allowedTime) {
        throw "Unsupported time filter in operation $($operation.operation)"
    }
}

$observationsByKey = @{}
$tokensById = @{}
$operationResults = New-Object System.Collections.ArrayList
$completedOperations = @{}
$startedAt = (Get-Date).ToString('o')
$runSessionState = @{
    authentication_state = 'unknown'
    authentication_checked_at = $null
    session_generations = 0
    next_request_id = 3
}

if ($Resume) {
    $priorManifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    if ([string]$priorManifest.plan_sha256 -cne $PlanHash) {
        throw 'Plan hash changed; refusing to resume into an incompatible run.'
    }
    $startedAt = [string]$priorManifest.started_at
    foreach ($result in @($priorManifest.operation_results)) {
        $null = $operationResults.Add([ordered]@{
            operation = [int]$result.operation
            query_key = [string]$result.query_key
            status = [string]$result.status
            returned = [int]$result.returned
            duration_seconds = [double]$result.duration_seconds
            error_category = [string]$result.error_category
            error_message = if (
                $result.PSObject.Properties.Name -contains 'error_message'
            ) { [string]$result.error_message } else { '' }
            applied_time_filter = [string]$result.applied_time_filter
            applied_sort_mode = [string]$result.applied_sort_mode
            fallback_used = [bool]$result.fallback_used
        })
        if ([string]$result.status -eq 'passed') {
            $completedOperations[[int]$result.operation] = $true
        }
    }
    if (Test-Path -LiteralPath $ObservationsJsonl) {
        foreach ($line in Get-Content -LiteralPath $ObservationsJsonl -Encoding UTF8) {
            if ([string]::IsNullOrWhiteSpace($line)) { continue }
            $row = ConvertFrom-CaseSensitiveJson -Json $line
            $key = "$($row['operation'])|$($row['feed_id'])"
            $observationsByKey[$key] = $row
        }
    }
    if (Test-Path -LiteralPath $LookupPath) {
        $lookup = ConvertFrom-CaseSensitiveJson -Json (
            Get-Content -LiteralPath $LookupPath -Raw -Encoding UTF8
        )
        foreach ($entry in @($lookup['entries'])) {
            $tokensById[[string]$entry['feed_id']] = [string]$entry['xsec_token']
        }
    }
}

function Save-RunCheckpoint {
    param(
        [Parameter(Mandatory)][string]$Status,
        [Parameter(Mandatory)][string]$StopReason
    )

    $observations = @(
        $observationsByKey.Values |
            Sort-Object `
                @{Expression={ [int]$_['operation'] }; Descending=$false},
                @{Expression={ [int]$_['result_rank'] }; Descending=$false}
    )
    @($observations | ForEach-Object {
        $_ | ConvertTo-Json -Compress -Depth 12
    }) | Set-Content -LiteralPath $ObservationsJsonl -Encoding UTF8

    @($observations | ForEach-Object {
        [pscustomobject]@{
            operation = $_['operation']
            query_key = $_['query_key']
            query_text = $_['query_text']
            query_group = $_['query_group']
            layer = $_['layer']
            time_filter = $_['time_filter']
            sort_mode = $_['sort_mode']
            planned_time_filter = $_['planned_time_filter']
            planned_sort_mode = $_['planned_sort_mode']
            fallback_used = $_['fallback_used']
            result_rank = $_['result_rank']
            feed_id = $_['feed_id']
            title = $_['title']
            author = $_['author']
            liked_count = $_['liked_count']
            collected_count = $_['collected_count']
            comment_count = $_['comment_count']
            shared_count = $_['shared_count']
            observed_at = $_['observed_at']
        }
    }) | Export-Csv `
        -LiteralPath $ObservationsCsv `
        -NoTypeInformation `
        -Encoding UTF8

    $lookupEntries = @(
        $tokensById.Keys |
            Sort-Object |
            ForEach-Object {
                [ordered]@{
                    feed_id = $_
                    xsec_token = [string]$tokensById[$_]
                }
            }
    )
    [ordered]@{
        schema_version = '1.0'
        run_id = $RunId
        entries = $lookupEntries
    } | ConvertTo-Json -Depth 8 |
        Set-Content -LiteralPath $LookupPath -Encoding UTF8

    $passed = @($operationResults | Where-Object {
        [string]$_['status'] -eq 'passed'
    }).Count
    $failed = @($operationResults | Where-Object {
        [string]$_['status'] -eq 'failed'
    }).Count
    $uniqueNotes = @($observations | ForEach-Object {
        [string]$_['feed_id']
    } | Sort-Object -Unique).Count

    [ordered]@{
        schema_version = '1.0'
        run_id = $RunId
        status = $Status
        stop_reason = $StopReason
        started_at = $startedAt
        updated_at = (Get-Date).ToString('o')
        completed_at = if ($Status -in @('completed', 'partial')) {
            (Get-Date).ToString('o')
        } else { $null }
        plan_path = $PlanPath
        plan_sha256 = $PlanHash
        plan_run_date = [string]$plan.run_date
        request_boundary = [ordered]@{
            allowed_tools = $AllowedToolCalls
            note_type = $TextImageNote
            delay_seconds = $DelaySeconds
            browser_mode = if ($VisibleBrowser) { 'visible' } else { 'headless' }
            session_lifecycle = 'per_run'
            max_transport_restarts_per_call = 1
            authentication_failure_behavior = 'checkpoint_and_pause'
        }
        authentication = [ordered]@{
            state = [string]$runSessionState.authentication_state
            checked_at = $runSessionState.authentication_checked_at
        }
        session = [ordered]@{
            generations = [int]$runSessionState.session_generations
            transport_restarts = if ($null -ne $sessionLease) {
                [int]$sessionLease.RestartCount
            } else { 0 }
        }
        counts = [ordered]@{
            operations_selected = $operations.Count
            operations_passed = $passed
            operations_failed = $failed
            observations = $observations.Count
            unique_notes = $uniqueNotes
        }
        operation_results = @($operationResults)
        outputs = [ordered]@{
            observations_jsonl = $ObservationsJsonl
            observations_csv = $ObservationsCsv
        }
        private_lookup_cache = $LookupPath
    } | ConvertTo-Json -Depth 15 |
        Set-Content -LiteralPath $ManifestPath -Encoding UTF8
}

$sessionLease = $null
$consecutiveErrors = 0
$stopReason = 'operations_completed'

try {
    Save-RunCheckpoint -Status 'running' -StopReason 'starting'
    $sessionLease = New-RadarSessionLease `
        -StartSession {
            $sessionStamp = Get-Date -Format 'yyyyMMddTHHmmssfff'
            $sessionNumber = [int]$runSessionState.session_generations + 1
            $sessionStdout = Join-Path $RuntimeLogs `
                "radar-$RunId-session$sessionNumber-$sessionStamp.stdout.log"
            $sessionStderr = Join-Path $RuntimeLogs `
                "radar-$RunId-session$sessionNumber-$sessionStamp.stderr.log"
            $startedSession = Start-ReadOnlyMcpSession `
                -StdoutPath $sessionStdout `
                -StderrPath $sessionStderr
            $runSessionState.authentication_state = 'authenticated'
            $runSessionState.authentication_checked_at = (Get-Date).ToString('o')
            $runSessionState.session_generations = $sessionNumber
            return $startedSession
        }.GetNewClosure() `
        -StopSession {
            param($Session)
            Stop-ReadOnlyMcpSession -Session $Session
        }.GetNewClosure()
    foreach ($operation in $operations) {
        $operationNumber = [int]$operation.operation
        if ($completedOperations.ContainsKey($operationNumber)) {
            continue
        }
        $priorResult = @($operationResults | Where-Object {
            [int]$_['operation'] -eq $operationNumber
        })
        foreach ($item in $priorResult) {
            $operationResults.Remove($item)
        }

        $started = Get-Date
        $status = 'passed'
        $errorCategory = ''
        $errorMessage = ''
        $returned = 0
        $appliedTimeFilter = [string]$operation.time_filter
        $appliedSortMode = [string]$operation.sort_mode
        $fallbackUsed = $false
        $fallbackReason = ''
        $pauseRequested = $false
        try {
            $attempts = New-Object System.Collections.ArrayList
            $null = $attempts.Add([ordered]@{
                role = 'planned'
                time_filter = [string]$operation.time_filter
                sort_mode = [string]$operation.sort_mode
                apply_filters = $true
            })
            if (
                [string]$operation.time_filter -cne $TextTimeUnlimited -or
                [string]$operation.sort_mode -cne $TextSortComprehensive
            ) {
                $null = $attempts.Add([ordered]@{
                    role = 'fallback'
                    time_filter = $TextTimeUnlimited
                    sort_mode = $TextSortComprehensive
                    apply_filters = $true
                })
            }

            $searchResult = $null
            $attemptErrors = New-Object System.Collections.ArrayList
            foreach ($attemptSpec in $attempts) {
                try {
                    $searchResult = Invoke-RadarSessionCall `
                        -Lease $sessionLease `
                        -MaxTransportRestarts 1 `
                        -Operation {
                            param($ActiveSession)
                            $requestId = [int]$runSessionState.next_request_id
                            $runSessionState.next_request_id = $requestId + 1
                            $searchArguments = @{
                                keyword = [string]$operation.query_text
                            }
                            if ([bool]$attemptSpec['apply_filters']) {
                                $searchArguments.filters = @{
                                    sort_by = [string]$attemptSpec['sort_mode']
                                    note_type = $TextImageNote
                                    publish_time = [string]$attemptSpec['time_filter']
                                }
                            }
                            Invoke-ReadOnlyTool `
                                -Name 'search_feeds' `
                                -Arguments $searchArguments `
                                -Id $requestId `
                                -SessionId ([string]$ActiveSession.SessionId)
                        }.GetNewClosure()
                    $appliedTimeFilter = [string]$attemptSpec['time_filter']
                    $appliedSortMode = [string]$attemptSpec['sort_mode']
                    $fallbackUsed = (
                        [string]$attemptSpec['role'] -eq 'fallback'
                    )
                    break
                }
                catch {
                    $attemptMessage = [string]$_.Exception.Message
                    $null = $attemptErrors.Add($attemptMessage)
                    $searchResult = $null
                    $attemptDisposition = Get-RadarSessionFailureDisposition `
                        -Message $attemptMessage
                    if ($attemptDisposition.Action -eq 'pause_run') {
                        $pauseRequested = $true
                        $stopReason = [string]$attemptDisposition.StopReason
                        break
                    }
                    # A provider search error is retained by its process after the
                    # browser lease closes. Preserve this operation and checkpoint
                    # pending keywords; a retry here would only replay the old error.
                    if ($attemptMessage -match '^search_feeds failed: 搜索Feeds失败:') {
                        $pauseRequested = $true
                        $stopReason = 'provider_search_lease_closed'
                        break
                    }
                    if ($attemptDisposition.Action -eq 'restart_session') {
                        $pauseRequested = $true
                        $stopReason = 'transport_unavailable'
                        break
                    }
                }
            }
            if (-not $searchResult -and -not $pauseRequested -and $attemptErrors.Count -gt 0) {
                $filterFailure = ($attemptErrors -join ' | ')
                if ($filterFailure -match 'apply search filters failed|筛选面板|context deadline exceeded') {
                    $null = $attempts.Add([ordered]@{
                        role = 'unfiltered_fallback'
                        time_filter = $TextFilterUnavailable
                        sort_mode = $TextFilterUnavailable
                        apply_filters = $false
                        fallback_reason = 'filter_panel_unavailable'
                    })
                    try {
                        $searchResult = Invoke-RadarSessionCall `
                            -Lease $sessionLease `
                            -MaxTransportRestarts 1 `
                            -Operation {
                                param($ActiveSession)
                                $requestId = [int]$runSessionState.next_request_id
                                $runSessionState.next_request_id = $requestId + 1
                                $searchArguments = @{
                                    keyword = [string]$operation.query_text
                                }
                                Invoke-ReadOnlyTool `
                                    -Name 'search_feeds' `
                                    -Arguments $searchArguments `
                                    -Id $requestId `
                                    -SessionId ([string]$ActiveSession.SessionId)
                            }.GetNewClosure()
                        $appliedTimeFilter = $TextFilterUnavailable
                        $appliedSortMode = $TextFilterUnavailable
                        $fallbackUsed = $true
                        $fallbackReason = 'filter_panel_unavailable'
                    }
                    catch {
                        $null = $attemptErrors.Add([string]$_.Exception.Message)
                        $lastDisposition = Get-RadarSessionFailureDisposition -Message ([string]$_.Exception.Message)
                        if ($lastDisposition.Action -eq 'pause_run') {
                            $pauseRequested = $true
                            $stopReason = [string]$lastDisposition.StopReason
                        } elseif ([string]$_.Exception.Message -match '^search_feeds failed: 搜索Feeds失败:') {
                            $pauseRequested = $true
                            $stopReason = 'provider_search_lease_closed'
                        }
                    }
                }
            }
            if (-not $searchResult) {
                throw ($attemptErrors -join ' | ')
            }
            $searchData = ConvertFrom-CaseSensitiveJson `
                -Json (Get-ToolText -ToolResult $searchResult)
            $feeds = @($searchData['feeds'])
            $returned = $feeds.Count
            $rank = 0
            foreach ($feed in $feeds) {
                $rank++
                $feedId = [string]$feed['id']
                $xsecToken = [string]$feed['xsecToken']
                if ([string]::IsNullOrWhiteSpace($feedId) -or
                    [string]::IsNullOrWhiteSpace($xsecToken)) {
                    continue
                }
                $card = $feed['noteCard']
                $user = $card['user']
                $interact = $card['interactInfo']
                $cover = $card['cover']
                $author = [string]$user['nickname']
                if ([string]::IsNullOrWhiteSpace($author)) {
                    $author = [string]$user['nickName']
                }
                $coverUrl = [string]$cover['urlDefault']
                if ([string]::IsNullOrWhiteSpace($coverUrl)) {
                    $coverUrl = [string]$cover['url']
                }
                $observedAt = (Get-Date).ToString('o')
                $likedRaw = [string]$interact['likedCount']
                $collectedRaw = [string]$interact['collectedCount']
                $commentRaw = [string]$interact['commentCount']
                $sharedRaw = [string]$interact['sharedCount']
                $row = [ordered]@{
                    schema_version = '1.0'
                    run_id = $RunId
                    operation = $operationNumber
                    query_key = [string]$operation.query_key
                    query_text = [string]$operation.query_text
                    query_group = [string]$operation.query_group
                    layer = [string]$operation.layer
                    time_filter = $appliedTimeFilter
                    sort_mode = $appliedSortMode
                    planned_time_filter = [string]$operation.time_filter
                    planned_sort_mode = [string]$operation.sort_mode
                    fallback_used = $fallbackUsed
                    fallback_reason = $fallbackReason
                    variant_role = [string]$operation.variant_role
                    result_rank = $rank
                    observed_at = $observedAt
                    feed_id = $feedId
                    canonical_url = "https://www.xiaohongshu.com/explore/$feedId"
                    title = [string]$card['displayTitle']
                    author_id = [string]$user['userId']
                    author = $author
                    note_type = [string]$card['type']
                    liked_count_raw = $likedRaw
                    liked_count = Convert-MetricToNumber $likedRaw
                    collected_count_raw = $collectedRaw
                    collected_count = Convert-MetricToNumber $collectedRaw
                    comment_count_raw = $commentRaw
                    comment_count = Convert-MetricToNumber $commentRaw
                    shared_count_raw = $sharedRaw
                    shared_count = Convert-MetricToNumber $sharedRaw
                    cover_url = $coverUrl
                }
                $observationsByKey["$operationNumber|$feedId"] = $row
                $tokensById[$feedId] = $xsecToken
            }
            $consecutiveErrors = 0
            $completedOperations[$operationNumber] = $true
        }
        catch {
            $status = 'failed'
            $message = [string]$_.Exception.Message
            $errorMessage = if ($message.Length -gt 500) {
                $message.Substring(0, 500)
            } else {
                $message
            }
            $disposition = Get-RadarSessionFailureDisposition -Message $message
            $errorCategory = [string]$disposition.Category
            if ($disposition.Action -eq 'pause_run') {
                $pauseRequested = $true
                $stopReason = [string]$disposition.StopReason
            }
            elseif ($disposition.Action -eq 'restart_session') {
                $pauseRequested = $true
                $stopReason = 'transport_unavailable'
            }
            if ($errorCategory -eq 'authentication') {
                $runSessionState.authentication_state = 'invalid'
                $runSessionState.authentication_checked_at = (Get-Date).ToString('o')
            }
            $consecutiveErrors++
        }

        $duration = [math]::Round(((Get-Date) - $started).TotalSeconds, 2)
        $null = $operationResults.Add([ordered]@{
            operation = $operationNumber
            query_key = [string]$operation.query_key
            status = $status
            returned = $returned
            duration_seconds = $duration
            error_category = $errorCategory
            error_message = $errorMessage
            applied_time_filter = $appliedTimeFilter
            applied_sort_mode = $appliedSortMode
            fallback_used = $fallbackUsed
            fallback_reason = $fallbackReason
        })
        Save-RunCheckpoint -Status 'running' -StopReason 'checkpoint'
        Write-Output ([pscustomobject]@{
            Operation = $operationNumber
            Query = [string]$operation.query_text
            Layer = [string]$operation.layer
            Filter = "$appliedTimeFilter + $appliedSortMode"
            FallbackUsed = $fallbackUsed
            Status = $status
            Returned = $returned
            UniqueNotes = @($observationsByKey.Values | ForEach-Object {
                [string]$_['feed_id']
            } | Sort-Object -Unique).Count
            DurationSeconds = $duration
            ErrorCategory = $errorCategory
            ErrorMessage = $errorMessage
        })

        if ($pauseRequested) {
            break
        }
        if ($consecutiveErrors -ge 2) {
            $stopReason = 'two_consecutive_errors'
            break
        }
        if ($DelaySeconds -gt 0) {
            Start-Sleep -Seconds $DelaySeconds
        }
    }

    $passedCount = @($operationResults | Where-Object {
        [string]$_['status'] -eq 'passed'
    }).Count
    $finalStatus = if ($passedCount -eq $operations.Count) {
        'completed'
    } elseif ($passedCount -gt 0) {
        'partial'
    } else {
        'failed'
    }
    Save-RunCheckpoint -Status $finalStatus -StopReason $stopReason
    $finalManifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    [pscustomobject]@{
        RunId = $RunId
        Status = $finalStatus
        OperationsSelected = $operations.Count
        OperationsPassed = $finalManifest.counts.operations_passed
        OperationsFailed = $finalManifest.counts.operations_failed
        Observations = $finalManifest.counts.observations
        UniqueNotes = $finalManifest.counts.unique_notes
        Manifest = $ManifestPath
    }
    if ($finalStatus -eq 'failed') { exit 2 }
}
finally {
    if ($null -ne $sessionLease) {
        Close-RadarSessionLease -Lease $sessionLease
    }
    else {
        Stop-IsolatedBrowserProcesses
    }
}

