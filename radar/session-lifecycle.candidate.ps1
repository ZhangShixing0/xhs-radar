$ErrorActionPreference = 'Stop'

function New-RadarSessionLease {
    param(
        [Parameter(Mandatory)][scriptblock]$StartSession,
        [Parameter(Mandatory)][scriptblock]$StopSession
    )

    return [pscustomobject]@{
        StartSession = $StartSession
        StopSession = $StopSession
        Session = $null
        Generation = 0
        RestartCount = 0
        Closed = $false
    }
}

function Get-RadarSession {
    param([Parameter(Mandatory)]$Lease)

    if ($Lease.Closed) {
        throw 'The radar session lease is closed.'
    }
    if ($null -eq $Lease.Session) {
        $session = & $Lease.StartSession
        if ($null -eq $session) {
            throw 'The radar session factory returned no session.'
        }
        $Lease.Session = $session
        $Lease.Generation = [int]$Lease.Generation + 1
    }
    return $Lease.Session
}

function Restart-RadarSession {
    param([Parameter(Mandatory)]$Lease)

    if ($Lease.Closed) {
        throw 'The radar session lease is closed.'
    }
    if ($null -ne $Lease.Session) {
        & $Lease.StopSession $Lease.Session
        $Lease.Session = $null
    }
    $Lease.RestartCount = [int]$Lease.RestartCount + 1
    return Get-RadarSession -Lease $Lease
}

function Close-RadarSessionLease {
    param([Parameter(Mandatory)]$Lease)

    if ($Lease.Closed) { return }
    try {
        if ($null -ne $Lease.Session) {
            & $Lease.StopSession $Lease.Session
            $Lease.Session = $null
        }
    }
    finally {
        $Lease.Closed = $true
    }
}

# Only controlled public status fields can leave this sanitizer.
# Never include host URLs, cookies, account IDs, provider errors or raw messages.
function Get-RadarControlledCheckTime {
    param($Value)
    if ($Value -isnot [string] -or $Value -cnotmatch '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$') {
        return $null
    }
    $parsed = [datetimeoffset]::MinValue
    $check = [regex]::Replace($Value, '(\.\d{7})\d+(?=Z|[+-])', '$1')
    if ([datetimeoffset]::TryParse($check, [ref]$parsed)) { return $Value }
    return $null
}

function Get-RadarSafeSiteCheckReport {
    param($Report, [int]$EnvelopeIndex, [string]$ParseState = 'parsed')
    $result = [ordered]@{
        envelope_index = $EnvelopeIndex
        parse_state = $ParseState
        status = $null
        is_logged_in = $null
        selected_site = $null
        checked_at = $null
        sites = @()
    }
    if ($null -eq $Report) { return [pscustomobject]$result }
    $names = @($Report.PSObject.Properties.Name)
    if ('status' -in $names -and $Report.status -is [string] -and
        $Report.status -cin @('confirmed', 'guest', 'unknown', 'stopped', 'save_failed', 'binding_invalidated')) {
        $result.status = $Report.status
    }
    if ('is_logged_in' -in $names -and $Report.is_logged_in -is [bool]) { $result.is_logged_in = $Report.is_logged_in }
    if ('selected_site' -in $names -and $Report.selected_site -is [string] -and
        $Report.selected_site -cin @('rednote', 'xiaohongshu')) { $result.selected_site = $Report.selected_site }
    if ('checked_at' -in $names) { $result.checked_at = Get-RadarControlledCheckTime -Value $Report.checked_at }
    if ('sites' -in $names -and $Report.sites -is [array]) {
        foreach ($site in $Report.sites) {
            $entry = [ordered]@{site=$null;status=$null;reason=$null;checked_at=$null}
            if ($null -ne $site) {
                $fields = @($site.PSObject.Properties.Name)
                if ('site' -in $fields -and $site.site -is [string] -and
                    $site.site -cin @('rednote', 'xiaohongshu')) { $entry.site = $site.site }
                if ('status' -in $fields -and $site.status -is [string] -and
                    $site.status -cin @('confirmed', 'guest', 'unknown', 'not_checked', 'challenge')) {
                    $entry.status = $site.status
                }
                if ('reason' -in $fields -and $site.reason -is [string] -and
                    $site.reason -cin @('', 'challenge', 'context_cancelled', 'context_deadline',
                        'readiness_timeout', 'site_mismatch', 'network_error', 'check_error', 'state_unconfirmed')) {
                    $entry.reason = $site.reason
                }
                if ('checked_at' -in $fields) { $entry.checked_at = Get-RadarControlledCheckTime -Value $site.checked_at }
            }
            $result.sites += [pscustomobject]$entry
        }
    }
    return [pscustomobject]$result
}

function Get-RadarSessionFailureDisposition {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Message)
    $diagnostics = @()
    $structured = Get-RadarSiteCheckDisposition -Message $Message -Diagnostics ([ref]$diagnostics)
    $result = Get-RadarSessionFailureDispositionCore -Message $Message -SiteCheckDisposition $structured
    if ($diagnostics.Count -gt 0) {
        $structuredCategory = if ($null -ne $structured) { $structured.Category } else { $null }
        $result | Add-Member -NotePropertyName StructuredCategory -NotePropertyValue $structuredCategory
        $result | Add-Member -NotePropertyName SiteCheckDiagnostics -NotePropertyValue @($diagnostics)
    }
    return $result
}

# Offline-testable handling for the provider's controlled status envelope.
# Never outputs the message, report, account values, or underlying error text.
function Get-RadarSiteCheckDisposition {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Message, [ref]$Diagnostics)

    $markers = [regex]::Matches($Message, '\bsite_check_result\s*=\s*')
    if ($markers.Count -eq 0) { return $null }

    $categories = New-Object System.Collections.ArrayList
    $reports = New-Object System.Collections.ArrayList
    $envelopeIndex = 0
    foreach ($marker in $markers) {
        $envelopeIndex++
        $start = $marker.Index + $marker.Length
        $end = -1
        $depth = 0
        $quoted = $false
        $escaped = $false
        if ($start -lt $Message.Length -and $Message[$start] -eq '{') {
            for ($index = $start; $index -lt $Message.Length; $index++) {
                $char = $Message[$index]
                if ($quoted) {
                    if ($escaped) { $escaped = $false }
                    elseif ($char -eq '\') { $escaped = $true }
                    elseif ($char -eq '"') { $quoted = $false }
                }
                elseif ($char -eq '"') { $quoted = $true }
                elseif ($char -eq '{') { $depth++ }
                elseif ($char -eq '}') {
                    $depth--
                    if ($depth -eq 0) { $end = $index; break }
                }
            }
        }
        if ($end -lt $start) {
            $null = $reports.Add((Get-RadarSafeSiteCheckReport -Report $null -EnvelopeIndex $envelopeIndex -ParseState 'incomplete_json'))
            $null = $categories.Add('authentication_unconfirmed')
            continue
        }
        try {
            $report = $Message.Substring($start, $end - $start + 1) |
                ConvertFrom-Json -ErrorAction Stop
        }
        catch {
            $null = $reports.Add((Get-RadarSafeSiteCheckReport -Report $null -EnvelopeIndex $envelopeIndex -ParseState 'invalid_json'))
            $null = $categories.Add('authentication_unconfirmed')
            continue
        }
        $null = $reports.Add((Get-RadarSafeSiteCheckReport -Report $report -EnvelopeIndex $envelopeIndex))
        $names = @($report.PSObject.Properties.Name)
        if ('status' -notin $names -or 'is_logged_in' -notin $names -or
            'sites' -notin $names -or $report.status -isnot [string] -or
            $report.is_logged_in -isnot [bool] -or $report.sites -isnot [array]) {
            $null = $categories.Add('authentication_unconfirmed')
            continue
        }
        $status = [string]$report.status
        $siteStates = @()
        $reasons = @()
        $validSites = $report.sites.Count -eq 2
        foreach ($site in $report.sites) {
            if ($null -eq $site) { $validSites = $false; continue }
            $siteNames = @($site.PSObject.Properties.Name)
            if ('status' -notin $siteNames -or $site.status -isnot [string]) {
                $validSites = $false
                continue
            }
            $siteStates += [string]$site.status
            if ([string]$site.status -cnotin @('confirmed', 'guest', 'unknown', 'not_checked', 'challenge')) {
                $validSites = $false
            }
            if ('reason' -in $siteNames) {
                if ($site.reason -isnot [string]) { $validSites = $false }
                else {
                    $reasons += [string]$site.reason
                    if ([string]$site.reason -cnotin @('', 'challenge', 'context_cancelled', 'context_deadline',
                            'readiness_timeout', 'site_mismatch', 'network_error', 'check_error', 'state_unconfirmed')) {
                        $validSites = $false
                    }
                }
            }
        }
        # A challenge is a stop signal even if another field is malformed.
        if ('challenge' -cin $siteStates -or 'challenge' -cin $reasons) {
            $null = $categories.Add('verification_challenge')
        }
        elseif (-not $validSites -or $status -cnotin @('confirmed', 'guest', 'unknown', 'stopped', 'save_failed', 'binding_invalidated')) {
            $null = $categories.Add('authentication_unconfirmed')
        }
        elseif ($status -ceq 'save_failed') {
            $null = $categories.Add('session_save_failed')
        }
        elseif ($status -ceq 'binding_invalidated') {
            $null = $categories.Add('site_binding_invalidated')
        }
        elseif ($status -ceq 'guest' -and -not $report.is_logged_in -and
                @($siteStates | Where-Object { $_ -cne 'guest' }).Count -eq 0) {
            $null = $categories.Add('authentication')
        }
        elseif ($status -ceq 'confirmed' -and $report.is_logged_in -and 'confirmed' -cin $siteStates) {
            # A successful report is not an error. Other concatenated reports
            # can still carry a stop signal; evaluate all envelopes first.
        }
        else {
            $null = $categories.Add('authentication_unconfirmed')
        }
    }

    if ($null -ne $Diagnostics) { $Diagnostics.Value = @($reports) }

    foreach ($category in @('verification_challenge', 'session_save_failed',
                            'site_binding_invalidated', 'authentication_unconfirmed', 'authentication')) {
        if ($category -in $categories) {
            $stopReason = if ($category -eq 'authentication') { 'authentication_required' } else { $category }
            return [pscustomobject]@{
                Category = $category; Action = 'pause_run'; StopReason = $stopReason
            }
        }
    }
    return $null
}


function Get-RadarSessionFailureDispositionCore {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Message, $SiteCheckDisposition)

    # Platform restrictions take precedence over transport/time-out fallbacks.
    $codePrefix = '(?:code|errcode|错误码)["''\s:=：]*'
    if ($Message -match ($codePrefix + '300012\b') -or
        $Message -match 'IP.{0,16}(限制|受限|封禁|blocked|restricted)|访问.{0,8}IP.{0,8}受限') {
        return [pscustomobject]@{
            Category = 'ip_restricted'; Action = 'pause_run'; StopReason = 'ip_restricted'
        }
    }
    if ($Message -match ($codePrefix + '300011\b') -or
        $Message -match '账号.{0,12}(异常|违规|受限|封禁)|不常见的浏览行为|与真人操作习惯|AI.{0,8}不规范|风控|account.{0,16}(risk|restricted|suspended)') {
        return [pscustomobject]@{
            Category = 'account_restricted'; Action = 'pause_run'; StopReason = 'account_restricted'
        }
    }
    if ($Message -match 'captcha|verification_challenge|verify slider|security verification|验证码|安全验证|滑块验证|(?:HTTP|status|code)["''\s:=：]*(461|471)\b') {
        return [pscustomobject]@{
            Category = 'verification_challenge'; Action = 'pause_run'; StopReason = 'verification_challenge'
        }
    }
    if ($Message -match '访问.{0,8}频繁|请求.{0,8}频繁|操作.{0,8}频繁|rate.?limit|too many requests|(?:HTTP|status|code)["''\s:=：]*429\b') {
        return [pscustomobject]@{
            Category = 'rate_limited'; Action = 'pause_run'; StopReason = 'rate_limited'
        }
    }
    if ($null -ne $siteCheckDisposition -and $siteCheckDisposition.Category -eq 'verification_challenge') {
        return $siteCheckDisposition
    }

    # Tool names such as check_login_status are context, not authentication evidence.
    # A failed navigation must not mark the persisted session invalid or start login.
    if ($Message -match 'net::ERR_TIMED_OUT|ERR_CONNECTION_TIMED_OUT|\bnetwork_timeout\b') {
        return [pscustomobject]@{
            Category = 'network_timeout'; Action = 'pause_run'; StopReason = 'network_timeout'
        }
    }
    if ($Message -match 'ERR_NETWORK_ACCESS_DENIED|network access denied|network_policy') {
        return [pscustomobject]@{
            Category = 'network_policy'; Action = 'pause_run'; StopReason = 'network_policy'
        }
    }

    # A per-site diagnostic must not overwrite the whole-report status.
    if ($null -ne $siteCheckDisposition) { return $siteCheckDisposition }

    if ($Message -match 'site_binding_invalidated') {
        return [pscustomobject]@{
            Category = 'site_binding_invalidated'; Action = 'pause_run'; StopReason = 'site_binding_invalidated'
        }
    }
    if ($Message -match 'site_check_save_failed') {
        return [pscustomobject]@{
            Category = 'session_save_failed'; Action = 'pause_run'; StopReason = 'session_save_failed'
        }
    }
    if ($Message -match 'site_check_inconclusive|site_check_unconfirmed') {
        return [pscustomobject]@{
            Category = 'authentication_unconfirmed'; Action = 'pause_run'; StopReason = 'authentication_unconfirmed'
        }
    }
    if ($Message -match 'readiness_timeout') {
        return [pscustomobject]@{
            Category = 'readiness_timeout'; Action = 'pause_run'; StopReason = 'readiness_timeout'
        }
    }
    if ($Message -match 'site_mismatch') {
        return [pscustomobject]@{
            Category = 'site_mismatch'; Action = 'pause_run'; StopReason = 'site_mismatch'
        }
    }

    if ($Message -match '\bauthentication_unconfirmed\b|Login validation did not confirm|authentication was not verified|authentication is inconclusive') {
        return [pscustomobject]@{
            Category = 'authentication_unconfirmed'; Action = 'pause_run'; StopReason = 'authentication_unconfirmed'
        }
    }
    if ($Message -match 'authentication_required|login_required|not[\s_-]+authenticated|unauthenticated|not logged in|未登录|登录失效|登录过期|请.{0,4}登录' -or
        $Message -match ($codePrefix + '-100\b')) {
        return [pscustomobject]@{
            Category = 'authentication'
            Action = 'pause_run'
            StopReason = 'authentication_required'
        }
    }
    if ($Message -match `
        'captcha|verification_challenge|verify slider|security verification') {
        return [pscustomobject]@{
            Category = 'verification_challenge'
            Action = 'pause_run'
            StopReason = 'verification_challenge'
        }
    }
    if ($Message -match 'already in use; refusing to attach') {
        return [pscustomobject]@{
            Category = 'port_conflict'
            Action = 'pause_run'
            StopReason = 'port_conflict'
        }
    }
    if ($Message -match `
        'connection refused|actively refused|unable to connect|remote server|MCP server did not listen|process.{0,20}exited|connection.{0,20}closed|forcibly closed') {
        return [pscustomobject]@{
            Category = 'transport'
            Action = 'restart_session'
            StopReason = ''
        }
    }
    if ($Message -match 'timeout|timed out|deadline') {
        return [pscustomobject]@{
            Category = 'timeout'
            Action = 'continue_operation'
            StopReason = ''
        }
    }
    if ($Message -match 'unsupported') {
        return [pscustomobject]@{
            Category = 'invalid_filter'
            Action = 'continue_operation'
            StopReason = ''
        }
    }
    return [pscustomobject]@{
        Category = 'read_failure'
        Action = 'continue_operation'
        StopReason = ''
    }
}

function Assert-RadarBusinessResponse {
    param([AllowEmptyString()][string]$Text)

    # Only inspect error envelopes / plain tool status, never text inside a note.
    $message = $Text
    if ($Text.TrimStart().StartsWith('{') -or $Text.TrimStart().StartsWith('[')) {
        try { $payload = $Text | ConvertFrom-Json -ErrorAction Stop }
        catch { return } # The caller's case-sensitive parser validates note data.
        $names = @($payload.PSObject.Properties.Name)
        $failed = ($names -contains 'success' -and $payload.success -eq $false)
        if ($names -contains 'code' -and [string]$payload.code -notin @('', '0', '200')) {
            $failed = $true
        }
        if (-not $failed) { return }
        $message = ''
        foreach ($key in @('code', 'message', 'msg', 'error')) {
            if ($names -contains $key) { $message += " $key=$($payload.$key)" }
        }
        $disposition = Get-RadarSessionFailureDisposition -Message $message
        if ($disposition.Action -eq 'pause_run') {
            throw "Platform request stopped: $($disposition.StopReason)"
        }
        throw 'Platform returned an unsuccessful business response.'
    }
    $disposition = Get-RadarSessionFailureDisposition -Message $message
    if ($disposition.Action -eq 'pause_run') {
        throw "Platform request stopped: $($disposition.StopReason)"
    }
}

function Invoke-RadarSessionCall {
    param(
        [Parameter(Mandatory)]$Lease,
        [Parameter(Mandatory)][scriptblock]$Operation,
        [ValidateRange(0, 3)][int]$MaxTransportRestarts = 1
    )

    $restartCount = 0
    while ($true) {
        try {
            $session = Get-RadarSession -Lease $Lease
            return & $Operation $session
        }
        catch {
            $disposition = Get-RadarSessionFailureDisposition `
                -Message ([string]$_.Exception.Message)
            if ($disposition.Action -eq 'restart_session' -and
                $restartCount -lt $MaxTransportRestarts) {
                $null = Restart-RadarSession -Lease $Lease
                $restartCount++
                continue
            }
            throw
        }
    }
}
