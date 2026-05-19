"""Generic edge-triggered process presence monitor."""

import time
from typing import Callable


class ProcessOpenWatcher:
    """
    Polls whether a process is running and calls *on_opened* once per
    open-edge, subject to a cooldown.

    Parameters
    ----------
    is_running:
        Zero-argument callable returning ``bool``.  Called every
        *poll_seconds*; must be cheap (e.g. a tasklist check).
    poll_seconds:
        How long to sleep between checks.  Default 2 s.
    cooldown_seconds:
        Minimum seconds between successive ``on_opened`` calls.
        Default 900 s (15 min).
    on_opened:
        Zero-argument callable invoked on each open-edge that passes the
        cooldown.  Called synchronously on the watcher thread; wrap in a
        ``threading.Thread`` if you need a non-blocking callback.
    reset_cooldown_on_close:
        When ``True`` (default), the cooldown timestamp is reset to zero
        when the process closes so the *next* open always fires immediately.
    """

    def __init__(
        self,
        is_running: Callable[[], bool],
        poll_seconds: float = 2.0,
        cooldown_seconds: float = 900.0,
        on_opened: Callable[[], None] = lambda: None,
        reset_cooldown_on_close: bool = True,
    ) -> None:
        self._is_running = is_running
        self._poll_seconds = poll_seconds
        self._cooldown_seconds = cooldown_seconds
        self._on_opened = on_opened
        self._reset_cooldown_on_close = reset_cooldown_on_close

    def run_forever(self) -> None:
        """Block forever, polling and firing *on_opened* on each open-edge."""
        was_running = self._is_running()
        last_trigger_ts: float = 0.0

        while True:
            time.sleep(self._poll_seconds)
            is_running = self._is_running()
            now = time.time()

            just_opened = is_running and not was_running
            just_closed = not is_running and was_running

            if just_closed and self._reset_cooldown_on_close:
                last_trigger_ts = 0.0

            if just_opened and (now - last_trigger_ts) >= self._cooldown_seconds:
                last_trigger_ts = now
                self._on_opened()

            was_running = is_running
