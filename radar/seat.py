"""Local checks before starting the shared Windows radar. No account or HTTP access."""
import ctypes
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import socket
import subprocess


def windows_environment(current=None):
    env = dict(os.environ if current is None else current)
    root = next((v for k, v in env.items() if k.casefold() in ("systemroot", "windir") and v), None)
    if root:
        system_modules = str(Path(root) / "System32/WindowsPowerShell/v1.0/Modules")
        key = next((k for k in env if k.casefold() == "psmodulepath"), "PSModulePath")
        previous = [p for p in env.get(key, "").split(";")
                    if p and os.path.normcase(p) != os.path.normcase(system_modules)]
        env[key] = ";".join([system_modules, *previous])
    return env


def provider_active():
    shell = shutil.which("powershell.exe")
    if not shell:
        raise ValueError("shared_seat_unknown_windows_required")
    probe = (
        "$ErrorActionPreference='Stop'; try { "
        "$services = @(Get-Process | Where-Object { $_.ProcessName -like 'xiaohongshu-mcp*' }); "
        "if ($services.Count) { exit 41 }; exit 0 } catch { exit 42 }"
    )
    result = subprocess.run([shell, "-NoProfile", "-NonInteractive", "-Command", probe],
                            capture_output=True, timeout=25, env=windows_environment())
    if result.returncode == 41:
        return True
    if result.returncode:
        raise ValueError("shared_seat_unknown_process_check")
    # Bind only, without listening or sending traffic. Other local port owners also block startup.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as check:
        check.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            check.bind(("127.0.0.1", 18060))
        except OSError as exc:
            if exc.winerror in (10013, 10048):
                return True
            raise ValueError("shared_seat_unknown_loopback_check") from exc
    return False


@contextmanager
def reserve():
    if os.name != "nt":
        raise ValueError("shared_seat_unknown_windows_required")
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    create = dll.CreateMutexW
    create.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    create.restype = ctypes.c_void_p
    wait = dll.WaitForSingleObject
    wait.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    wait.restype = ctypes.c_uint32
    dll.ReleaseMutex.argtypes = [ctypes.c_void_p]
    dll.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = create(None, False, "Local\\S2HandoffSharedRadar")
    if not handle:
        raise ValueError("shared_seat_unknown_lock")
    owned = False
    try:
        result = wait(handle, 0)
        owned = result in (0, 0x80)  # available or abandoned by a departed process
        if result == 0x102:
            raise ValueError("shared_seat_busy")
        if not owned:
            raise ValueError("shared_seat_unknown_lock")
        if provider_active():
            raise ValueError("shared_seat_busy")
        yield
    finally:
        if owned:
            dll.ReleaseMutex(handle)
        dll.CloseHandle(handle)
