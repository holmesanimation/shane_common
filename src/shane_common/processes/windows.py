"""Generic Windows process discovery and window enable/disable helpers.

On non-Windows platforms every function is a safe no-op that returns an
empty collection or False so callers need no platform guards.
"""

import os
import subprocess

if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


# ---------------------------------------------------------------------------
# Process discovery
# ---------------------------------------------------------------------------

def list_process_pids(image_name: str) -> set:
    """Return the set of integer PIDs for processes matching *image_name*.

    Uses ``tasklist /FO CSV`` for reliable CSV parsing.
    Returns an empty set on non-Windows or on any error.
    """
    if os.name != "nt":
        return set()
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=5,
        )
        pids: set = set()
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip('"') for p in line.split('","')]
            if len(parts) >= 2:
                try:
                    pids.add(int(parts[1]))
                except ValueError:
                    pass
        return pids
    except Exception:
        return set()


def is_process_running(image_name: str) -> bool:
    """Return True if at least one process with *image_name* is running.

    Uses ``tasklist /FI`` which is fast for a simple presence check.
    Returns False on non-Windows or on any error.
    """
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}"],
            capture_output=True,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=5,
        )
        return image_name.lower() in result.stdout.lower()
    except Exception:
        return False


def taskkill_processes(image_names) -> None:
    """Kill each named process with ``taskkill /F``.  Silently ignores errors.

    No-op on non-Windows.
    """
    if os.name != "nt":
        return
    for name in image_names:
        try:
            subprocess.run(
                ["taskkill", "/IM", name, "/F"],
                creationflags=subprocess.CREATE_NO_WINDOW,
                capture_output=True,
            )
        except Exception:
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
