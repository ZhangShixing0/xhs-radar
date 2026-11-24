# Radar collectors only. Dot-source after xhs-local/common.ps1.
# No login executable, runtime profile, site preference or global setting is changed.
if ([string]::IsNullOrWhiteSpace([string]$ToolRoot)) {
    throw 'Radar provider binding requires xhs-local/common.ps1 first.'
}
$MainVersionRoot = Join-Path $ToolRoot 'patched-readonly-radar-20261118-search-v5'
$MainExe = Join-Path $MainVersionRoot 'xiaohongshu-mcp-radar-readonly.exe'
$ExpectedMainSha256 = 'B02E17F7D9E75B439C90DC00F76EA73AF8C3EA164529CA8C1D64B55A0F1EEAFF'
if ($ExpectedMainSha256 -notmatch '^[A-F0-9]{64}$') {
    throw 'Radar provider package is unsealed: build SHA256 is missing.'
}
