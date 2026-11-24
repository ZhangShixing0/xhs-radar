"""Derive private copies of reviewed native collectors with owner-only lifecycle handling."""
import hashlib
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile


SEARCH_SHA = "a24770dc980d3b0f7783bc577e5a8f6b2db77e88fa63986a5b271fb359ab4e87"
DETAIL_SHA = "4b5ec7ee73fe2357d478cd0de03c79723e3ff1f76f89d9a30bb48935c32a3ee4"

# Assign a fresh, private job to the actual process handle returned by Start-Process.
# Chrome launched after assignment belongs to this job; a child whose ownership cannot
# be established is deliberately left alone. No lookup by profile path or recycled PID.
HELPERS = r'''
$script:S2OwnedProvider = $null
$script:S2OwnedJob = [IntPtr]::Zero
function Register-S2OwnedProvider($OwnedProcess) {
    $script:S2OwnedProvider = $OwnedProcess
    $providerHandle = $OwnedProcess.Handle
    try {
        if (-not ('S2Handoff.PrivateJob' -as [type])) {
            Add-Type -ErrorAction Stop -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
namespace S2Handoff {
    public static class PrivateJob {
        [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr a, string n);
        [DllImport("kernel32.dll")] static extern bool AssignProcessToJobObject(IntPtr j, IntPtr p);
        [DllImport("kernel32.dll")] static extern bool TerminateJobObject(IntPtr j, uint code);
        [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr h);
        public static IntPtr Bind(IntPtr p) {
            IntPtr h = CreateJobObject(IntPtr.Zero, null);
            if (h != IntPtr.Zero && !AssignProcessToJobObject(h, p)) { CloseHandle(h); return IntPtr.Zero; }
            return h;
        }
        public static void Finish(IntPtr h) {
            if (h != IntPtr.Zero) { try { TerminateJobObject(h, 1); } finally { CloseHandle(h); } }
        }
    }
}
'@
        }
        $script:S2OwnedJob = [S2Handoff.PrivateJob]::Bind($providerHandle)
    } catch { $script:S2OwnedJob = [IntPtr]::Zero }
}
function Stop-S2OwnedProvider($OwnedProcess) {
    if ($null -eq $OwnedProcess -or -not [object]::ReferenceEquals($OwnedProcess, $script:S2OwnedProvider)) { return }
    try {
        if ($script:S2OwnedJob -ne [IntPtr]::Zero) {
            [S2Handoff.PrivateJob]::Finish($script:S2OwnedJob)
        }
        if (-not $OwnedProcess.HasExited) { $OwnedProcess.Kill() }
        $null = $OwnedProcess.WaitForExit(3000)
    } catch {} finally {
        $script:S2OwnedJob = [IntPtr]::Zero
        $script:S2OwnedProvider = $null
    }
}
function Assert-S2ListenerOwned {
    $p = $script:S2OwnedProvider
    if ($null -eq $p -or $p.HasExited) { throw 'shared_seat_owner_unknown' }
    $netstat = Join-Path $env:SystemRoot 'System32/netstat.exe'
    $rows = @(& $netstat -ano -p TCP | ForEach-Object {
        if ($_ -match '^\s*TCP\s+127\.0\.0\.1:18060\s+0\.0\.0\.0:0\s+\S+\s+(\d+)\s*$') { $Matches[1] }
    })
    if ($LASTEXITCODE -ne 0 -or $rows.Count -ne 1 -or [int]$rows[0] -ne $p.Id -or $p.HasExited) {
        throw 'shared_seat_owner_unknown'
    }
}
'''


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError("Changed collector lifecycle. Review the installed release before running.")
    return text.replace(old, new, 1)


def transform(text, mode):
    begin = text.index("function Stop-IsolatedBrowserProcesses {")
    end = text.index("function Normalize-ProcessPathVariable {", begin)
    text = text[:begin] + HELPERS + "\n" + text[end:]
    head = "function Start-ReadOnlyMcpSession {" if mode == "search" else "function Start-ReadOnlySession {"
    stop = "function Stop-ReadOnlyMcpSession {" if mode == "search" else "function Stop-ReadOnlySession {"
    opening = text.index(head)
    catch = text.index("\n    catch {", opening)
    stop_pos = text.index(stop, catch)
    end = text.index("\n}", stop_pos) + 2
    cleanup = ("\n    catch {\n        Stop-S2OwnedProvider $process\n        throw\n    }\n}\n\n"
               + stop + "\n    param($Session)\n    if ($null -ne $Session) { Stop-S2OwnedProvider $Session.Process }\n}")
    text = text[:catch] + cleanup + text[end:]
    text = replace_once(text, "            -PassThru", "            -PassThru\n        Register-S2OwnedProvider $process")
    text = replace_once(text, "    $headers = @{ Accept =", "    Assert-S2ListenerOwned\n    $headers = @{ Accept =")
    if mode == "search":
        text = replace_once(text, "    else {\n        Stop-IsolatedBrowserProcesses\n    }", "")
    else:
        # The previous note may have yielded the port during its low-frequency delay.
        # Pause before counting the next planned detail if a competing service is active.
        text = replace_once(text, "    $attempted++", """    if (@(Get-Process | Where-Object { $_.ProcessName -like 'xiaohongshu-mcp*' }).Count) {
        $stopReason = 'shared_seat_busy'
        break
    }
    $attempted++""")
    if "Stop-IsolatedBrowserProcesses" in text or re.search(r"\bStop-Process\b", text):
        raise ValueError("An unbound process cleanup remains in the private collector")
    return text


def literal(path):
    return "'" + str(path).replace("'", "''") + "'"


def immutable_script(directory, prefix, text):
    contents = text.replace("\n", "\r\n").encode("utf-8-sig")
    digest = hashlib.sha256(contents).hexdigest()
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / (prefix + "-" + digest + ".ps1")
    if not file.is_file():
        with NamedTemporaryFile(dir=directory, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(contents)
        try:
            try:
                os.link(temp, file)
            except FileExistsError:
                pass
        finally:
            temp.unlink(missing_ok=True)
    if file.read_bytes() != contents:
        raise ValueError("Owner-bound private script content changed")
    return file


def private_collector(source, expected, directory, mode):
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Source collector digest differs from the reviewed binding")
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    # Keep dependencies pointing to their original host directory when moving this copy.
    text = text.replace("$PSScriptRoot", literal(source.parent))
    return immutable_script(directory, mode, transform(text, mode))


def private_wrapper(wrapper, expected, engine, directory, mode):
    raw = wrapper.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("Default wrapper digest changed")
    if mode == "search":
        variable, filename, expected_collector = "Collector", "collect-radar-run.ps1", SEARCH_SHA
    else:
        variable, filename, expected_collector = "DetailCollector", "collect-qualified-details.ps1", DETAIL_SHA
    candidate = private_collector(engine / "scripts/xhs-radar" / filename, expected_collector, directory, mode)
    text = raw.decode("utf-8-sig").replace("\r\n", "\n")
    text = text.replace("$PSScriptRoot", literal(wrapper.parent))
    marker = "$RunsRoot = Join-Path $EngineRoot"
    actions = "@('search','resume')" if mode == "search" else "@('details')"
    guard = ("if ($Action -notin " + actions + ") { throw 'private_wrapper_action_mismatch' }\n"
             + "$" + variable + " = " + literal(candidate) + "\n"
             + "if ((Get-FileHash -LiteralPath $" + variable + ").Hash -ine '"
             + hashlib.sha256(candidate.read_bytes()).hexdigest() + "') { throw 'owner_bound_digest_changed' }\n")
    return immutable_script(directory, "run-" + mode, replace_once(text, marker, guard + marker))
