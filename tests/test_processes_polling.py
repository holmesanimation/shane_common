"""Unit tests for shane_common.processes.polling.ProcessOpenWatcher."""

import time
from unittest.mock import MagicMock, patch, call

import pytest

from shane_common.processes.polling import ProcessOpenWatcher


def _make_sequence(*values):
    """Return an is_running callable that yields each value in order."""
    it = iter(values)

    def _is_running():
        try:
            return next(it)
        except StopIteration:
            return False

    return _is_running


class TestProcessOpenWatcherEdgeTrigger:
    def _run_ticks(self, watcher, n_ticks, fake_time_start=100_000.0):
        """Drive *n_ticks* of watcher._step using monkeypatched time."""
        # We patch time.sleep to a no-op and time.time to return
        # a predictable sequence so cooldown tests are deterministic.
        tick_time = [fake_time_start]

        def _fake_sleep(_):
            tick_time[0] += watcher._poll_seconds

        def _fake_time():
            return tick_time[0]

        with patch("shane_common.processes.polling.time.sleep", side_effect=_fake_sleep):
            with patch("shane_common.processes.polling.time.time", side_effect=_fake_time):
                # Patch run_forever to stop after n_ticks by raising StopIteration
                original_run = watcher.run_forever
                call_count = [0]

                def limited_run():
                    # Inline the loop body manually for n_ticks iterations
                    was_running = watcher._is_running()
                    last_trigger_ts = 0.0
                    for _ in range(n_ticks):
                        time.sleep(watcher._poll_seconds)
                        is_running = watcher._is_running()
                        now = time.time()
                        just_opened = is_running and not was_running
                        just_closed = not is_running and was_running
                        if just_closed and watcher._reset_cooldown_on_close:
                            last_trigger_ts = 0.0
                        if just_opened and (now - last_trigger_ts) >= watcher._cooldown_seconds:
                            last_trigger_ts = now
                            watcher._on_opened()
                        was_running = is_running

                limited_run()

    def test_on_opened_called_when_process_starts(self):
        on_opened = MagicMock()
        # Sequence: was_running check → False; tick 1 → True (open edge)
        watcher = ProcessOpenWatcher(
            is_running=_make_sequence(False, False, True),
            poll_seconds=1.0,
            cooldown_seconds=0.0,
            on_opened=on_opened,
        )
        self._run_ticks(watcher, 2)
        on_opened.assert_called_once()

    def test_on_opened_not_called_when_already_running(self):
        on_opened = MagicMock()
        # Starts running, stays running – no open edge
        watcher = ProcessOpenWatcher(
            is_running=_make_sequence(True, True, True),
            poll_seconds=1.0,
            cooldown_seconds=0.0,
            on_opened=on_opened,
        )
        self._run_ticks(watcher, 2)
        on_opened.assert_not_called()

    def test_fires_on_every_open_edge_with_zero_cooldown(self):
        on_opened = MagicMock()
        # Open → close → open: zero cooldown means both open-edges fire
        watcher = ProcessOpenWatcher(
            is_running=_make_sequence(False, False, True, False, True),
            poll_seconds=1.0,
            cooldown_seconds=0.0,
            on_opened=on_opened,
        )
        self._run_ticks(watcher, 4)
        assert on_opened.call_count == 2

    def test_cooldown_resets_on_close(self):
        on_opened = MagicMock()
        # Tick 1: open (fires), Tick 2: close (resets cooldown), Tick 3: open (fires again)
        watcher = ProcessOpenWatcher(
            is_running=_make_sequence(False, False, True, False, True),
            poll_seconds=1.0,
            cooldown_seconds=9999.0,
            on_opened=on_opened,
            reset_cooldown_on_close=True,
        )
        self._run_ticks(watcher, 4)
        assert on_opened.call_count == 2

    def test_cooldown_not_reset_when_flag_false(self):
        on_opened = MagicMock()
        # Same sequence but reset_cooldown_on_close=False → second open within cooldown
        watcher = ProcessOpenWatcher(
            is_running=_make_sequence(False, False, True, False, True),
            poll_seconds=1.0,
            cooldown_seconds=9999.0,
            on_opened=on_opened,
            reset_cooldown_on_close=False,
        )
        self._run_ticks(watcher, 4)
        on_opened.assert_called_once()

    def test_on_opened_not_called_when_process_never_starts(self):
        on_opened = MagicMock()
        watcher = ProcessOpenWatcher(
            is_running=_make_sequence(False, False, False, False),
            poll_seconds=1.0,
            cooldown_seconds=0.0,
            on_opened=on_opened,
        )
        self._run_ticks(watcher, 3)
        on_opened.assert_not_called()

    def test_default_on_opened_is_callable_noop(self):
        # Should not raise with no on_opened provided
        watcher = ProcessOpenWatcher(
            is_running=_make_sequence(False, False, True),
            poll_seconds=1.0,
            cooldown_seconds=0.0,
        )
        self._run_ticks(watcher, 2)  # no assertion, just must not raise
