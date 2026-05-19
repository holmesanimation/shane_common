# shane_common/tests/ui/log_viewer/test_log_row_emitter.py
"""
Tests for LogRowEmitterProtocol, LogRowEmitter (Qt), and CallbackLogRowEmitter.

CallbackLogRowEmitter tests do NOT require Qt.
LogRowEmitter (Qt) tests are skipped if PySide6 is unavailable.
"""
from __future__ import annotations

import threading
from typing import List

import pytest

from shane_common.ui.log_viewer.log_row import LogRow
from shane_common.ui.log_viewer.log_row_emitter import (
    CallbackLogRowEmitter,
    LogRowEmitterProtocol,
)

_SAMPLE_ROW = LogRow(
    ts=1_700_000_000.0,
    kind="system.start",
    severity="INFO",
    code="system.start",
    instrument=None,
    message="Session started",
)


# ===================================================================== #
# CallbackLogRowEmitter — no Qt required
# ===================================================================== #

class TestCallbackLogRowEmitter:
    def test_emit_calls_callback(self):
        received: List[LogRow] = []
        emitter = CallbackLogRowEmitter(on_row=received.append)
        emitter.emit_row(_SAMPLE_ROW)
        assert received == [_SAMPLE_ROW]

    def test_buffer_populated(self):
        emitter = CallbackLogRowEmitter(on_row=lambda r: None)
        emitter.emit_row(_SAMPLE_ROW)
        assert _SAMPLE_ROW in emitter.drain_buffer()

    def test_drain_buffer_returns_all(self):
        rows = [
            LogRow(ts=float(i), kind="k", severity="DEBUG", code="k",
                   instrument=None, message=str(i))
            for i in range(5)
        ]
        emitter = CallbackLogRowEmitter(on_row=lambda r: None)
        for r in rows:
            emitter.emit_row(r)
        assert emitter.drain_buffer() == rows

    def test_drain_buffer_does_not_clear(self):
        emitter = CallbackLogRowEmitter(on_row=lambda r: None)
        emitter.emit_row(_SAMPLE_ROW)
        first = emitter.drain_buffer()
        second = emitter.drain_buffer()
        assert first == second

    def test_maxlen_cap(self):
        collected: List[LogRow] = []
        emitter = CallbackLogRowEmitter(on_row=collected.append)
        # Internal deque maxlen is 50_000 — just verify overflow trims correctly
        # by patching maxlen to a small value via direct access.
        from collections import deque
        emitter._buffer = deque(maxlen=3)
        rows = [
            LogRow(ts=float(i), kind="k", severity="DEBUG", code="k",
                   instrument=None, message=str(i))
            for i in range(5)
        ]
        for r in rows:
            emitter.emit_row(r)
        buffered = emitter.drain_buffer()
        assert len(buffered) == 3
        assert buffered == rows[-3:]

    def test_thread_safe_emit(self):
        received: List[LogRow] = []
        lock = threading.Lock()

        def on_row(r: LogRow) -> None:
            with lock:
                received.append(r)

        emitter = CallbackLogRowEmitter(on_row=on_row)
        threads = [
            threading.Thread(target=emitter.emit_row, args=(_SAMPLE_ROW,))
            for _ in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(received) == 20

    def test_satisfies_protocol(self):
        emitter = CallbackLogRowEmitter(on_row=lambda r: None)
        assert isinstance(emitter, LogRowEmitterProtocol)


# ===================================================================== #
# LogRowEmitter (Qt) — requires PySide6
# ===================================================================== #

try:
    from PySide6.QtWidgets import QApplication
    import sys
    _qt_available = True
except ImportError:
    _qt_available = False

pytestmark_qt = pytest.mark.skipif(
    not _qt_available, reason="PySide6 not available"
)


@pytest.fixture(scope="module")
def qt_app():
    if not _qt_available:
        pytest.skip("PySide6 not available")
    app = QApplication.instance() or QApplication([])
    yield app


@pytestmark_qt
class TestLogRowEmitter:
    def test_emit_fires_signal(self, qt_app):
        from shane_common.ui.log_viewer.log_row_emitter import LogRowEmitter
        received: List[LogRow] = []
        emitter = LogRowEmitter()
        emitter.log_row_appended.connect(received.append)
        emitter.emit_row(_SAMPLE_ROW)
        assert received == [_SAMPLE_ROW]

    def test_drain_buffer_returns_rows(self, qt_app):
        from shane_common.ui.log_viewer.log_row_emitter import LogRowEmitter
        emitter = LogRowEmitter()
        emitter.emit_row(_SAMPLE_ROW)
        assert _SAMPLE_ROW in emitter.drain_buffer()

    def test_satisfies_protocol(self, qt_app):
        from shane_common.ui.log_viewer.log_row_emitter import LogRowEmitter
        emitter = LogRowEmitter()
        assert isinstance(emitter, LogRowEmitterProtocol)

    def test_thread_safe_buffer(self, qt_app):
        from shane_common.ui.log_viewer.log_row_emitter import LogRowEmitter
        emitter = LogRowEmitter()
        threads = [
            threading.Thread(target=emitter.emit_row, args=(_SAMPLE_ROW,))
            for _ in range(20)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(emitter.drain_buffer()) == 20
