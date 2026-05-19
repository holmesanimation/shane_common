# shane_common/ui/log_viewer/log_row_emitter.py
"""
LogRowEmitterProtocol and concrete implementations.

Provides the decoupling seam between log normalisation and UI transports.
Swapping emitters (Qt Signal, callback, API publisher) requires only a
1-line constructor change in the sink; BaseLogNormalizerSink never changes.
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Callable, List, runtime_checkable

from typing import Protocol

from shane_common.ui.log_viewer.log_row import LogRow


@runtime_checkable
class LogRowEmitterProtocol(Protocol):
    """Protocol satisfied by all LogRow emitter implementations."""

    def emit_row(self, row: LogRow) -> None: ...
    def drain_buffer(self) -> List[LogRow]: ...


# ------------------------------------------------------------------ #
# Qt implementation
# ------------------------------------------------------------------ #

class LogRowEmitter:
    """
    Qt-based LogRow emitter.

    Fires ``log_row_appended`` PySide6 Signal on each ``emit_row`` call
    and maintains a ring buffer for seeding newly opened windows.

    Import is deferred so that modules importing only ``CallbackLogRowEmitter``
    do not require PySide6 to be installed.
    """

    def __init__(self) -> None:
        # Import Qt here so that non-Qt callers (tests, review_trader) don't
        # need PySide6 installed.
        from PySide6.QtCore import QObject, Signal  # noqa: PLC0415

        class _Signals(QObject):
            log_row_appended = Signal(object)

        self._signals = _Signals()
        self.log_row_appended = self._signals.log_row_appended

        self._buffer: deque[LogRow] = deque(maxlen=50_000)
        self._buf_lock = threading.Lock()

    def emit_row(self, row: LogRow) -> None:
        with self._buf_lock:
            self._buffer.append(row)
        self._signals.log_row_appended.emit(row)

    def drain_buffer(self) -> List[LogRow]:
        with self._buf_lock:
            return list(self._buffer)


# ------------------------------------------------------------------ #
# Pure-Python implementation (no Qt required)
# ------------------------------------------------------------------ #

class CallbackLogRowEmitter:
    """
    Pure-Python LogRow emitter backed by a callback.

    No Qt dependency — suitable for ``review_trader`` dock, unit tests,
    and any context where a Qt event loop is not available.
    """

    def __init__(self, on_row: Callable[[LogRow], None]) -> None:
        self._on_row = on_row
        self._buffer: deque[LogRow] = deque(maxlen=50_000)
        self._buf_lock = threading.Lock()

    def emit_row(self, row: LogRow) -> None:
        with self._buf_lock:
            self._buffer.append(row)
        self._on_row(row)

    def drain_buffer(self) -> List[LogRow]:
        with self._buf_lock:
            return list(self._buffer)
