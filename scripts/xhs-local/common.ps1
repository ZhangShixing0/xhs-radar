$ErrorActionPreference = 'Stop'

if ([string]::IsNullOrWhiteSpace($env:XHS_RADAR_ENGINE_ROOT)) {
    throw 'The radar entry point must provide the shared device root.'
}
$WorkspaceRoot = (Resolve-Path -LiteralPath $env:XHS_RADAR_ENGINE_ROOT).Path

$ToolRoot = Join-Path $WorkspaceRoot '.local-tools\xiaohongshu-mcp'
$RuntimeRoot = Join-Path $ToolRoot '.runtime'
$RuntimeLocalAppData = Join-Path $RuntimeRoot 'localappdata'
$RuntimeUserDataDir = Join-Path $RuntimeRoot 'user-data'
$RuntimeCookies = Join-Path $RuntimeRoot 'cookies.json'
$RuntimeLogs = Join-Path $RuntimeRoot 'logs'
$RuntimeReadOnlySite = Join-Path $RuntimeRoot 'readonly-site.json'


function Assert-VerifiedExecutable {
    param(
        [Parameter(Mandatory)]
        [string]$LiteralPath,

        [Parameter(Mandatory)]
        [string]$ExpectedSha256
    )

    if (-not (Test-Path -LiteralPath $LiteralPath -PathType Leaf)) {
        throw "Required executable is missing: $LiteralPath"
    }

    $actual = (Get-FileHash `
        -Algorithm SHA256 `
        -LiteralPath $LiteralPath).Hash

    if ($actual -cne $ExpectedSha256) {
        throw "SHA256 mismatch for $LiteralPath"
    }
}

function Initialize-IsolatedRuntime {
    New-Item -ItemType Directory -Force -Path `
        $RuntimeRoot,
        $RuntimeLocalAppData,
        $RuntimeUserDataDir,
        $RuntimeLogs | Out-Null

    $env:LOCALAPPDATA = $RuntimeLocalAppData
    $env:COOKIES_PATH = $RuntimeCookies
    $env:XHS_USER_DATA_DIR = $RuntimeUserDataDir
    # Load the preferred site; the provider checks both sites and binds a confirmed one.
    if ([string]::IsNullOrWhiteSpace($env:XHS_BASE_URL) -and
        (Test-Path -LiteralPath $RuntimeReadOnlySite -PathType Leaf)) {
        $xhsReadOnlySite = Get-Content -LiteralPath $RuntimeReadOnlySite -Raw -Encoding UTF8 | ConvertFrom-Json
        $xhsReadOnlyBase = [string]$xhsReadOnlySite.base_url
        if ($xhsReadOnlyBase -notin @('https://www.xiaohongshu.com', 'https://www.rednote.com')) {
            throw 'Invalid saved read-only site selection.'
        }
        $env:XHS_BASE_URL = $xhsReadOnlyBase
    }
}
