"""Generic Windows process discovery and window enable/disable helpers.

On non-Windows platforms every function is a safe no-op that returns an
empty collection or False so callers need no platform guards.
"""

import os
import subprocess
import threading
import time
import traceback


_SLOW_WINDOWS_PROCESS_CALL_THRESHOLD_MS = 50.0
_PROCESS_SNAPSHOT_CACHE_TTL_S = 0.25

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _TH32CS_SNAPPROCESS = 0x00000002
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _MAX_PATH = 260

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * _MAX_PATH),
        ]

    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32FirstW.restype = wintypes.BOOL
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _kernel32.Process32NextW.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _process_snapshot_cache_lock = threading.Lock()
    _process_snapshot_cache: tuple[float, dict[str, set[int]]] = (0.0, {})


# ---------------------------------------------------------------------------
# Process discovery
# ---------------------------------------------------------------------------


def _log_slow_windows_process_call(operation: str, started_at: float, *, target: str = "") -> None:
    elapsed_ms = (time.perf_counter() - started_at) * 1000.0
    if elapsed_ms < _SLOW_WINDOWS_PROCESS_CALL_THRESHOLD_MS:
        return
    suffix = f" target={target}" if target else ""
    print(
        f"[WindowsProcesses] slow operation: {operation} took {elapsed_ms:.1f} ms{suffix}",
        flush=True,
    )


def _snapshot_processes_by_image_name() -> dict[str, set[int]]:
    if os.name != "nt":
        return {}

    global _process_snapshot_cache
    now = time.monotonic()
    with _process_snapshot_cache_lock:
        cached_at, cached_snapshot = _process_snapshot_cache
        if (now - cached_at) < _PROCESS_SNAPSHOT_CACHE_TTL_S:
            return cached_snapshot

    snapshot: dict[str, set[int]] = {}
    handle = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if handle == _INVALID_HANDLE_VALUE:
        return snapshot

    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    try:
        if not _kernel32.Process32FirstW(handle, ctypes.byref(entry)):
            return snapshot

        while True:
            name = str(entry.szExeFile).strip().lower()
            if name:
                snapshot.setdefault(name, set()).add(int(entry.th32ProcessID))
            if not _kernel32.Process32NextW(handle, ctypes.byref(entry)):
                break
    finally:
        _kernel32.CloseHandle(handle)

    with _process_snapshot_cache_lock:
        _process_snapshot_cache = (now, snapshot)
    return snapshot

def list_process_pids(image_name: str) -> set:
    """Return the set of integer PIDs for processes matching *image_name*.

    Uses a native Toolhelp snapshot on Windows to avoid blocking subprocess calls.
    Returns an empty set on non-Windows or on any error.
    """
    if os.name != "nt":
        return set()
    started_at = time.perf_counter()
    try:
        pids = _snapshot_processes_by_image_name().get(str(image_name).strip().lower(), set())
        _log_slow_windows_process_call("list_process_pids", started_at, target=image_name)
        return set(pids)
    except Exception:
        _log_slow_windows_process_call("list_process_pids", started_at, target=image_name)
        return set()


def has_visible_window(image_name: str) -> bool:
    """Return True if *image_name* owns at least one visible top-level window.

    This is stricter than :func:`is_process_running` and avoids treating
    background browser helper processes as an active foreground browser.
    """
    if os.name != "nt":
        return False
    try:
        pids = list_process_pids(image_name)
        if not pids:
            return False
        return bool(enum_visible_windows_for_pids(pids))
    except Exception:
        traceback.print_exc()
        return False


def is_process_running(image_name: str) -> bool:
    """Return True if at least one process with *image_name* is running.

    Uses the native process snapshot shared with :func:`list_process_pids`.
    Returns False on non-Windows or on any error.
    """
    if os.name != "nt":
        return False
    started_at = time.perf_counter()
    try:
        running = bool(_snapshot_processes_by_image_name().get(str(image_name).strip().lower()))
        _log_slow_windows_process_call("is_process_running", started_at, target=image_name)
        return running
    except Exception:
        _log_slow_windows_process_call("is_process_running", started_at, target=image_name)
        return False


def taskkill_processes(image_names) -> None:
    """Kill each named process with ``taskkill /F``.  Silently ignores errors.

    No-op on non-Windows.
    """
    if os.name != "nt":
        return
    for name in image_names:
        started_at = time.perf_counter()
        try:
            subprocess.run(
                ["taskkill", "/IM", name, "/F"],
                creationflags=subprocess.CREATE_NO_WINDOW,
                capture_output=True,
            )
            _log_slow_windows_process_call("taskkill_processes", started_at, target=str(name))
        except Exception:
            _log_slow_windows_process_call("taskkill_processes", started_at, target=str(name))
            pass


# ---------------------------------------------------------------------------
# Window enable / disable (Win32)
# ---------------------------------------------------------------------------

def enum_visible_windows_for_pids(pids) -> list:
    """Return HWNDs of all visible top-level windows belonging to *pids*.

    Returns an empty list on non-Windows.
    """
    if os.name != "nt":
        return []
    hwnds: list = []

    @_EnumWindowsProc
    def _proc(hwnd, lParam):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            if pid.value in pids and _user32.IsWindowVisible(hwnd):
                hwnds.append(hwnd)
        except Exception:
            pass
        return True

    _user32.EnumWindows(_proc, 0)
    return hwnds


def disable_windows(hwnds) -> list:
    """Disable each HWND and return only the HWNDs that were actually disabled.

    Returns an empty list on non-Windows.
    """
    if os.name != "nt":
        return []
    disabled = []
    for hwnd in hwnds:
        try:
            if _user32.IsWindowEnabled(hwnd):
                _user32.EnableWindow(hwnd, False)
                disabled.append(hwnd)
        except Exception:
            pass
    return disabled


def enable_windows(hwnds) -> None:
    """Re-enable each HWND.  No-op on non-Windows."""
    if os.name != "nt":
        return
    for hwnd in hwnds:
        try:
            _user32.EnableWindow(hwnd, True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Window minimize / restore (Win32)
# ---------------------------------------------------------------------------

_SW_MINIMIZE = 6
_SW_RESTORE = 9


def minimize_windows(hwnds) -> list:
    """Minimize each HWND that is currently visible and not already minimized.

    Returns the list of HWNDs that were actually minimized so callers can
    restore them later.  No-op / returns [] on non-Windows.
    """
    if os.name != "nt":
        return []
    minimized = []
    for hwnd in hwnds:
        try:
            if _user32.IsWindowVisible(hwnd) and not _user32.IsIconic(hwnd):
                _user32.ShowWindow(hwnd, _SW_MINIMIZE)
                minimized.append(hwnd)
        except Exception:
            pass
    return minimized


def restore_windows(hwnds) -> None:
    """Restore each HWND that was previously minimized.  No-op on non-Windows."""
    if os.name != "nt":
        return
    for hwnd in hwnds:
        try:
            _user32.ShowWindow(hwnd, _SW_RESTORE)
        except Exception:
            pass
