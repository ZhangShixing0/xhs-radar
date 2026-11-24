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

function Get-RadarSessionFailureDisposition {
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Message)

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
    # Tool names such as check_login_status are context, not authentication evidence.
    # A failed navigation must not mark the persisted session invalid or start login.
    if ($Message -match 'net::ERR_TIMED_OUT|ERR_CONNECTION_TIMED_OUT|\bnetwork_timeout\b') {
        return [pscustomobject]@{
            Category = 'network_timeout'; Action = 'pause_run'; StopReason = 'network_timeout'
        }
    }
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
    if ($Message -match 'ERR_NETWORK_ACCESS_DENIED|network access denied|network_policy') {
        return [pscustomobject]@{
            Category = 'network_policy'; Action = 'pause_run'; StopReason = 'network_policy'
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
