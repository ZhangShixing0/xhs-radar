param(
    [Parameter(Mandatory)]
    [string]$RunId,

    [int]$BatchSize = 0,
    [int]$DelaySeconds = 8,
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

$TextLoggedIn = ConvertFrom-CodePoints @(0x5DF2, 0x767B, 0x5F55)

if ($RunId -notmatch '^[a-z0-9][a-z0-9-]{5,80}$') {
    throw 'RunId must contain only lowercase letters, digits, and hyphens.'
}

$RunRoot = Join-Path $WorkspaceRoot "data\xhs-radar\runs\$RunId"
$SelectionPath = Join-Path $RunRoot 'detail-selection-v2.json'
$DetailsJsonl = Join-Path $RunRoot 'qualified-details-v2.jsonl'
$DetailsCsv = Join-Path $RunRoot 'qualified-details-v2.csv'
$SummaryPath = Join-Path $RunRoot 'qualified-details-v2-summary.json'
$LookupPath = Join-Path $RuntimeRoot `
    "research-cache\radar-runs\$RunId-lookup.json"

foreach ($required in @($SelectionPath, $LookupPath)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required input is missing: $required"
    }
}
$SelectionHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $SelectionPath).Hash

function Assert-RadarDetailIdentity {
    param(
        [Parameter(Mandatory)][string]$ExpectedNoteId,
        [AllowEmptyString()][string]$ExpectedAuthorId,
        [Parameter(Mandatory)][System.Collections.IDictionary]$Note
    )
    $returnedNoteId = [string]$Note['noteId']
    if ([string]::IsNullOrWhiteSpace($returnedNoteId) -or $returnedNoteId -cne $ExpectedNoteId) {
        throw 'detail_note_identity_mismatch'
    }
    if ([string]::IsNullOrWhiteSpace($ExpectedAuthorId)) {
        throw 'detail_expected_author_unknown'
    }
    $returnedUser = $Note['user']
    $returnedAuthorId = if ($null -ne $returnedUser) { [string]$returnedUser['userId'] } else { '' }
    if ([string]::IsNullOrWhiteSpace($returnedAuthorId)) {
        throw 'detail_returned_author_unknown'
    }
    if ($returnedAuthorId -cne $ExpectedAuthorId) {
        throw 'detail_author_identity_mismatch'
    }
    return [ordered]@{
        status = 'matched'
        expected_note_id = $ExpectedNoteId
        returned_note_id = $returnedNoteId
        expected_author_id = $ExpectedAuthorId
        returned_author_id = $returnedAuthorId
        note_id_matches = $true
        author_id_matches = $true
    }
}

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
    try { return $content | ConvertFrom-Json }
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

function Test-LocalPort {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect('127.0.0.1', 18060, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(500)) { return $false }
        $client.EndConnect($pending)
        return $true
    }
    catch { return $false }
    finally { $client.Dispose() }
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

$AllowedToolCalls = @('check_login_status', 'get_feed_detail')

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
        params = @{ name = $Name; arguments = $Arguments }
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

function Start-ReadOnlySession {
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
            if (Test-LocalPort) { $listening = $true; break }
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
                    name = 'codex-xhs-qualified-detail-collector'
                    version = '2.0.0'
                }
            }
        }
        $initialize = ConvertFrom-McpHttpResponse -Response $initializeHttp
        if ($initialize.PSObject.Properties.Name -contains 'error' -and
            $initialize.error) {
            throw 'MCP initialize failed.'
        }
        $sessionId = [string]$initializeHttp.Headers['Mcp-Session-Id']
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
        if ((Get-ToolText -ToolResult $loginResult) -notmatch [regex]::Escape($TextLoggedIn)) {
            Assert-RadarBusinessResponse -Text (Get-ToolText -ToolResult $loginResult)
            throw 'Login validation did not confirm an authenticated session.'
        }
        return [ordered]@{ Process = $process; SessionId = $sessionId }
    }
    catch {
        if ($process -and -not $process.HasExited) {
            Stop-Process -Id $process.Id -ErrorAction SilentlyContinue
            $process.WaitForExit(3000) | Out-Null
            if (-not $process.HasExited) {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
        }
        Stop-IsolatedBrowserProcesses
        throw
    }
}

function Stop-ReadOnlySession {
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

$selection = ConvertFrom-CaseSensitiveJson -Json (
    Get-Content -LiteralPath $SelectionPath -Raw -Encoding UTF8
)
$selected = @($selection['selected'])
if ($selected.Count -eq 0) { throw 'Detail selection is empty.' }
foreach ($candidate in $selected) {
    if ([long]$candidate['collected_count'] -lt [long]$selection['gate']['min_collected']) {
        throw 'Selection contains a candidate below the hard collection gate.'
    }
}

$lookup = ConvertFrom-CaseSensitiveJson -Json (
    Get-Content -LiteralPath $LookupPath -Raw -Encoding UTF8
)
$tokensById = @{}
foreach ($entry in @($lookup['entries'])) {
    $tokensById[[string]$entry['feed_id']] = [string]$entry['xsec_token']
}

$detailsById = @{}
if ($Resume -and ((Test-Path -LiteralPath $DetailsJsonl) -or (Test-Path -LiteralPath $SummaryPath))) {
    if (-not (Test-Path -LiteralPath $SummaryPath)) {
        throw 'Detail summary is missing; cannot verify the original selection.'
    }
    if (Test-Path -LiteralPath $SummaryPath) {
        $prior = Get-Content -LiteralPath $SummaryPath -Raw -Encoding UTF8 |
            ConvertFrom-Json
        if ([string]$prior.selection_sha256 -cne $SelectionHash) {
            throw 'Selection changed; refusing to resume incompatible detail output.'
        }
    }
    foreach ($line in $(if (Test-Path -LiteralPath $DetailsJsonl) {
        Get-Content -LiteralPath $DetailsJsonl -Encoding UTF8
    })) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $detail = ConvertFrom-CaseSensitiveJson -Json $line
        if ([string]$detail['feed_id'] -notin @($selected | ForEach-Object { [string]$_['feed_id'] })) {
            throw 'Detail output contains a note outside the original selection.'
        }
        $detailsById[[string]$detail['feed_id']] = $detail
    }
}
elseif ((Test-Path -LiteralPath $DetailsJsonl) -or
        (Test-Path -LiteralPath $SummaryPath)) {
    throw 'Detail output already exists; use -Resume.'
}

$runResults = New-Object System.Collections.ArrayList
$errors = New-Object System.Collections.ArrayList
$attempted = 0
$succeeded = 0
$consecutiveErrors = 0
$stopReason = 'selection_completed'
$pauseRequested = $false

function Save-Checkpoint {
    param([Parameter(Mandatory)][string]$Status)
    $orderedDetails = @(
        $selected | ForEach-Object {
            $id = [string]$_['feed_id']
            if ($detailsById.ContainsKey($id)) { $detailsById[$id] }
        }
    )
    @($orderedDetails | ForEach-Object {
        $_ | ConvertTo-Json -Compress -Depth 20
    }) | Set-Content -LiteralPath $DetailsJsonl -Encoding UTF8
    @($orderedDetails | ForEach-Object {
        [pscustomobject]@{
            feed_id = $_['feed_id']
            title = $_['title']
            author = $_['author']
            topic_name = $_['topic_name']
            selection_rank = $_['selection_rank']
            selection_collected_count = $_['selection_collected_count']
            detail_collected_count = $_['collected_count']
            liked_count = $_['liked_count']
            comment_count = $_['comment_count']
            image_count = $_['image_count']
            returned_comment_count = $_['returned_comment_count']
            description_length = ([string]$_['description']).Length
            canonical_url = $_['canonical_url']
        }
    }) | Export-Csv `
        -LiteralPath $DetailsCsv `
        -NoTypeInformation `
        -Encoding UTF8
    [ordered]@{
        schema_version = '2.0'
        run_id = $RunId
        status = $Status
        stop_reason = $stopReason
        updated_at = (Get-Date).ToString('o')
        selection_sha256 = $SelectionHash
        request_boundary = [ordered]@{
            allowed_tools = $AllowedToolCalls
            load_all_comments = $false
            image_downloads = $false
            browser_mode = if ($VisibleBrowser) { 'visible' } else { 'headless' }
        }
        counts = [ordered]@{
            selected = $selected.Count
            completed = $detailsById.Count
            attempted_this_run = $attempted
            succeeded_this_run = $succeeded
            failed_this_run = $errors.Count
            pending = $selected.Count - $detailsById.Count
        }
        run_results = @($runResults)
        errors = @($errors)
        outputs = [ordered]@{
            details_jsonl = $DetailsJsonl
            details_csv = $DetailsCsv
        }
    } | ConvertTo-Json -Depth 15 |
        Set-Content -LiteralPath $SummaryPath -Encoding UTF8
}

Save-Checkpoint -Status 'running'
foreach ($candidate in $selected) {
    if ($BatchSize -gt 0 -and $attempted -ge $BatchSize) {
        $stopReason = 'batch_size_reached'
        break
    }
    $feedId = [string]$candidate['feed_id']
    if ($detailsById.ContainsKey($feedId)) { continue }
    $attempted++
    $started = Get-Date
    $status = 'passed'
    $errorCategory = ''
    $session = $null
    try {
        $token = [string]$tokensById[$feedId]
        if ([string]::IsNullOrWhiteSpace($token)) {
            throw 'Private lookup token is missing.'
        }
        $stamp = Get-Date -Format 'yyyyMMddTHHmmssfff'
        $stdout = Join-Path $RuntimeLogs `
            "qualified-detail-$RunId-$feedId-$stamp.stdout.log"
        $stderr = Join-Path $RuntimeLogs `
            "qualified-detail-$RunId-$feedId-$stamp.stderr.log"
        $session = Start-ReadOnlySession `
            -StdoutPath $stdout `
            -StderrPath $stderr
        $result = Invoke-ReadOnlyTool `
            -Name 'get_feed_detail' `
            -Arguments @{
                feed_id = $feedId
                xsec_token = $token
                load_all_comments = $false
            } `
            -Id 3 `
            -SessionId ([string]$session.SessionId)
        $data = ConvertFrom-CaseSensitiveJson `
            -Json (Get-ToolText -ToolResult $result)
        $note = $data['data']['note']
        if (-not $note) { throw 'Detail response did not contain a note.' }
        $identityChecks = Assert-RadarDetailIdentity `
            -ExpectedNoteId $feedId `
            -ExpectedAuthorId ([string]$candidate['author_id']) `
            -Note $note
        $comments = $data['data']['comments']
        $user = $note['user']
        $interact = $note['interactInfo']
        $author = [string]$user['nickname']
        if ([string]::IsNullOrWhiteSpace($author)) {
            $author = [string]$user['nickName']
        }
        $images = @(
            @($note['imageList']) | ForEach-Object {
                [ordered]@{
                    width = [int]$_['width']
                    height = [int]$_['height']
                    url = [string]$_['urlDefault']
                    preview_url = [string]$_['urlPre']
                }
            }
        )
        $commentSignals = @(
            @($comments['list']) | ForEach-Object {
                $content = [string]$_['content']
                if (-not [string]::IsNullOrWhiteSpace($content)) {
                    [ordered]@{
                        content = $content
                        liked_count = Convert-MetricToNumber ([string]$_['likeCount'])
                    }
                }
            }
        )
        $detail = [ordered]@{
            feed_id = $feedId
            note_id = [string]$note['noteId']
            identity_checks = $identityChecks
            canonical_url = "https://www.xiaohongshu.com/explore/$feedId"
            selection_rank = [int]$candidate['selection_rank']
            selection_reason = [string]$candidate['selection_reason']
            selection_collected_count = [long]$candidate['collected_count']
            qualification_tier = [string]$candidate['qualification_tier']
            topic_slug = [string]$candidate['topic_slug']
            topic_name = [string]$candidate['topic_name']
            source_queries = @($candidate['source_queries'])
            title = [string]$note['title']
            description = [string]$note['desc']
            note_type = [string]$note['type']
            published_at_unix_ms = [long]$note['time']
            author_id = [string]$user['userId']
            author = $author
            liked_count_raw = [string]$interact['likedCount']
            liked_count = Convert-MetricToNumber ([string]$interact['likedCount'])
            collected_count_raw = [string]$interact['collectedCount']
            collected_count = Convert-MetricToNumber ([string]$interact['collectedCount'])
            comment_count_raw = [string]$interact['commentCount']
            comment_count = Convert-MetricToNumber ([string]$interact['commentCount'])
            shared_count_raw = [string]$interact['sharedCount']
            shared_count = Convert-MetricToNumber ([string]$interact['sharedCount'])
            image_count = $images.Count
            images = $images
            returned_comment_count = $commentSignals.Count
            comment_signals = $commentSignals
            captured_at = (Get-Date).ToString('o')
        }
        $detailsById[$feedId] = $detail
        $succeeded++
        $consecutiveErrors = 0
    }
    catch {
        $status = 'failed'
        $message = [string]$_.Exception.Message
        $disposition = Get-RadarSessionFailureDisposition -Message $message
        $errorCategory = [string]$disposition.Category
        if ($disposition.Action -eq 'pause_run') {
            $pauseRequested = $true
            $stopReason = [string]$disposition.StopReason
        }
        elseif ($message -match '^detail_(note_identity_mismatch|expected_author_unknown|returned_author_unknown|author_identity_mismatch)$') {
            $errorCategory = $message
        }
        elseif ($message -match 'token') {
            $errorCategory = 'token_missing_or_expired'
        }
        $null = $errors.Add([ordered]@{
            feed_id = $feedId
            category = $errorCategory
        })
        $consecutiveErrors++
    }
    finally {
        Stop-ReadOnlySession -Session $session
    }
    $duration = [math]::Round(((Get-Date) - $started).TotalSeconds, 2)
    $null = $runResults.Add([ordered]@{
        feed_id = $feedId
        status = $status
        duration_seconds = $duration
        error_category = $errorCategory
    })
    Save-Checkpoint -Status 'running'
    Write-Output ([pscustomobject]@{
        FeedId = $feedId
        Status = $status
        Completed = $detailsById.Count
        Selected = $selected.Count
        DurationSeconds = $duration
        ErrorCategory = $errorCategory
    })
    if ($pauseRequested) { break }
    if ($consecutiveErrors -ge 2) {
        $stopReason = 'two_consecutive_errors'
        break
    }
    if ($DelaySeconds -gt 0) { Start-Sleep -Seconds $DelaySeconds }
}

$finalStatus = if ($detailsById.Count -eq $selected.Count) {
    'completed'
}
elseif ($detailsById.Count -gt 0) {
    'partial'
}
else {
    'failed'
}
Save-Checkpoint -Status $finalStatus
[pscustomobject]@{
    RunId = $RunId
    Status = $finalStatus
    Selected = $selected.Count
    Completed = $detailsById.Count
    AttemptedThisRun = $attempted
    FailedThisRun = $errors.Count
    Summary = $SummaryPath
}
if ($finalStatus -eq 'failed') { exit 2 }
