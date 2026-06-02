"""Unit tests for shane_common.processes.windows.

Windows-specific branches are tested via mocking so the suite runs on all
platforms.  Non-Windows no-op behaviour is validated unconditionally.
"""

import os
import subprocess
from unittest.mock import MagicMock, patch, call

import pytest

from shane_common.processes.windows import (
    list_process_pids,
    has_visible_window,
    is_process_running,
    taskkill_processes,
    enum_visible_windows_for_pids,
    disable_windows,
    enable_windows,
)


# ---------------------------------------------------------------------------
# Non-Windows no-op behaviour (always runs)
# ---------------------------------------------------------------------------

class TestNonWindowsNoOps:
    """Patch os.name to 'posix' and verify all functions return safe defaults."""

    def test_list_process_pids_returns_empty_set(self):
        with patch("shane_common.processes.windows.os.name", "posix"):
            assert list_process_pids("chrome.exe") == set()

    def test_is_process_running_returns_false(self):
        with patch("shane_common.processes.windows.os.name", "posix"):
            assert is_process_running("chrome.exe") is False

    def test_has_visible_window_returns_false(self):
        with patch("shane_common.processes.windows.os.name", "posix"):
            assert has_visible_window("chrome.exe") is False

    def test_taskkill_processes_is_noop(self):
        with patch("shane_common.processes.windows.os.name", "posix"):
            # Should not raise and should not call subprocess
            with patch("shane_common.processes.windows.subprocess.run") as mock_run:
                taskkill_processes(["chrome.exe"])
                mock_run.assert_not_called()

    def test_enum_visible_windows_returns_empty_list(self):
        with patch("shane_common.processes.windows.os.name", "posix"):
            assert enum_visible_windows_for_pids({1, 2, 3}) == []

    def test_disable_windows_returns_empty_list(self):
        with patch("shane_common.processes.windows.os.name", "posix"):
            assert disable_windows([1, 2, 3]) == []

    def test_enable_windows_is_noop(self):
        with patch("shane_common.processes.windows.os.name", "posix"):
            enable_windows([1, 2, 3])  # should not raise


# ---------------------------------------------------------------------------
# list_process_pids — subprocess output parsing
# ---------------------------------------------------------------------------

_TASKLIST_CSV_CHROME = (
    '"chrome.exe","1234","Console","1","100 K"\r\n'
    '"chrome.exe","5678","Console","1","200 K"\r\n'
)
_TASKLIST_CSV_NO_MATCH = "INFO: No tasks are running which match the specified criteria.\r\n"


class TestListProcessPids:
    def _run(self, stdout):
        mock_result = MagicMock()
        mock_result.stdout = stdout
        with patch("shane_common.processes.windows.os.name", "nt"):
            with patch("shane_common.processes.windows.subprocess.run", return_value=mock_result):
                return list_process_pids("chrome.exe")

    def test_parses_multiple_pids(self):
        result = self._run(_TASKLIST_CSV_CHROME)
        assert result == {1234, 5678}

    def test_returns_empty_on_no_match(self):
        result = self._run(_TASKLIST_CSV_NO_MATCH)
        assert result == set()

    def test_returns_empty_on_subprocess_exception(self):
        with patch("shane_common.processes.windows.os.name", "nt"):
            with patch("shane_common.processes.windows.subprocess.run", side_effect=OSError):
                assert list_process_pids("chrome.exe") == set()

    def test_returns_empty_on_blank_output(self):
        assert self._run("") == set()


# ---------------------------------------------------------------------------
# is_process_running — simple tasklist output check
# ---------------------------------------------------------------------------

class TestIsProcessRunning:
    def _run(self, stdout):
        mock_result = MagicMock()
        mock_result.stdout = stdout
        with patch("shane_common.processes.windows.os.name", "nt"):
            with patch("shane_common.processes.windows.subprocess.run", return_value=mock_result):
                return is_process_running("chrome.exe")

    def test_returns_true_when_name_in_output(self):
        assert self._run("chrome.exe\r\n1234\r\n") is True

    def test_case_insensitive(self):
        assert self._run("CHROME.EXE\r\n") is True

    def test_returns_false_when_not_in_output(self):
        assert self._run("notepad.exe\r\n") is False

    def test_returns_false_on_exception(self):
        with patch("shane_common.processes.windows.os.name", "nt"):
            with patch("shane_common.processes.windows.subprocess.run", side_effect=OSError):
                assert is_process_running("chrome.exe") is False


# ---------------------------------------------------------------------------
# has_visible_window — visible browser window presence
# ---------------------------------------------------------------------------

class TestHasVisibleWindow:
    def test_returns_true_when_visible_window_found(self):
        with patch("shane_common.processes.windows.os.name", "nt"):
            with patch("shane_common.processes.windows.list_process_pids", return_value={1234}):
                with patch(
                    "shane_common.processes.windows.enum_visible_windows_for_pids",
                    return_value=[1001],
                ):
                    assert has_visible_window("chrome.exe") is True

    def test_returns_false_when_only_background_processes_exist(self):
        with patch("shane_common.processes.windows.os.name", "nt"):
            with patch("shane_common.processes.windows.list_process_pids", return_value={1234}):
                with patch(
                    "shane_common.processes.windows.enum_visible_windows_for_pids",
                    return_value=[],
                ):
                    assert has_visible_window("msedge.exe") is False

    def test_returns_false_when_pid_lookup_fails(self):
        with patch("shane_common.processes.windows.os.name", "nt"):
            with patch("shane_common.processes.windows.list_process_pids", side_effect=OSError):
                assert has_visible_window("chrome.exe") is False


# ---------------------------------------------------------------------------
# taskkill_processes — subprocess call verification
# ---------------------------------------------------------------------------

class TestTaskkillProcesses:
    def test_calls_taskkill_for_each_name(self):
        with patch("shane_common.processes.windows.os.name", "nt"):
            with patch("shane_common.processes.windows.subprocess.run") as mock_run:
                taskkill_processes(["chrome.exe", "msedge.exe"])
                assert mock_run.call_count == 2

    def test_silent_on_exception(self):
        with patch("shane_common.processes.windows.os.name", "nt"):
            with patch("shane_common.processes.windows.subprocess.run", side_effect=OSError):
                taskkill_processes(["chrome.exe"])  # must not raise
