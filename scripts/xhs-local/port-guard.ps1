Set-StrictMode -Version 2.0

function Test-LocalTcpPort {
    param(
        [Parameter(Mandatory)]
        [ValidateRange(1, 65535)]
        [int]$Port
    )

    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $pending = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $pending.AsyncWaitHandle.WaitOne(500)) {
            return $false
        }
        $client.EndConnect($pending)
        return $true
    }
    catch {
        return $false
    }
    finally {
        $client.Dispose()
    }
}

function Assert-LocalTcpPortFree {
    param(
        [Parameter(Mandatory)]
        [ValidateRange(1, 65535)]
        [int]$Port
    )

    if (Test-LocalTcpPort -Port $Port) {
        throw "Port $Port is already in use; refusing to attach."
    }
}
