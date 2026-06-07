from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from shane_common.ipc.python.traffic_recorder import TrafficRecorder


class IpcMonitorDialog(QDialog):
    """Two-pane IPC traffic monitor with debug send buttons.

    Parameters
    ----------
    recorder:
        ``TrafficRecorder`` whose ring buffer is polled every 500 ms.
    send_fn:
        Optional callable ``(msg: dict) -> None`` used by the debug buttons
        to broadcast a message to all connected clients (server-side) or send
        it directly (client-side).  When ``None`` the debug buttons are
        disabled.
    parent:
        Qt parent widget.
    """

    def __init__(
        self,
        recorder: TrafficRecorder,
        send_fn: Optional[Callable[[dict], None]] = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("IPC Monitor")
        self.resize(720, 620)
        self._recorder = recorder
        self._send_fn = send_fn
        self._records_cache: list[dict] = []

        root = QVBoxLayout(self)
        root.setSpacing(4)

        # ── Debug toolbar ───────────────────────────────────────────────
        toolbar = QWidget(self)
        tbar_layout = QHBoxLayout(toolbar)
        tbar_layout.setContentsMargins(0, 0, 0, 0)
        tbar_layout.setSpacing(6)

        self._btn_ping = QPushButton("Send Ping", toolbar)
        self._btn_ping.setEnabled(send_fn is not None)
        self._btn_ping.clicked.connect(self._send_ping)
        tbar_layout.addWidget(self._btn_ping)

        self._btn_hello = QPushButton("Send Hello", toolbar)
        self._btn_hello.setEnabled(send_fn is not None)
        self._btn_hello.clicked.connect(self._send_hello)
        tbar_layout.addWidget(self._btn_hello)

        self._btn_heartbeat = QPushButton("Send Heartbeat", toolbar)
        self._btn_heartbeat.setEnabled(send_fn is not None)
        self._btn_heartbeat.clicked.connect(self._send_heartbeat)
        tbar_layout.addWidget(self._btn_heartbeat)

        tbar_layout.addStretch(1)

        self._btn_clear = QPushButton("Clear", toolbar)
        self._btn_clear.clicked.connect(self._clear)
        tbar_layout.addWidget(self._btn_clear)

        root.addWidget(toolbar)

        # ── Traffic panes (vertical split: Input top, Output bottom) ───
        splitter = QSplitter(Qt.Orientation.Vertical, self)
        root.addWidget(splitter, stretch=2)

        in_panel = QWidget()
        in_layout = QVBoxLayout(in_panel)
        in_layout.setContentsMargins(0, 0, 0, 0)
        in_layout.setSpacing(2)
        in_layout.addWidget(QLabel("Input  (incoming from clients)"))
        self._in_list = QListWidget()
        in_layout.addWidget(self._in_list)
        splitter.addWidget(in_panel)

        out_panel = QWidget()
        out_layout = QVBoxLayout(out_panel)
        out_layout.setContentsMargins(0, 0, 0, 0)
        out_layout.setSpacing(2)
        out_layout.addWidget(QLabel("Output  (outgoing to clients)"))
        self._out_list = QListWidget()
        out_layout.addWidget(self._out_list)
        splitter.addWidget(out_panel)

        splitter.setSizes([280, 180])

        # ── Detail pane ─────────────────────────────────────────────────
        root.addWidget(QLabel("Payload detail"))
        self._detail = QPlainTextEdit(self)
        self._detail.setReadOnly(True)
        self._detail.setMaximumHeight(160)
        root.addWidget(self._detail)

        self._in_list.itemClicked.connect(self._show_detail)
        self._out_list.itemClicked.connect(self._show_detail)

        # ── Refresh timer ───────────────────────────────────────────────
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    def _refresh(self) -> None:
        self._records_cache = self._recorder.get_recent(200)
        in_sb  = self._in_list.verticalScrollBar()
        out_sb = self._out_list.verticalScrollBar()
        in_at_bottom  = in_sb.value()  >= in_sb.maximum()  - 4
        out_at_bottom = out_sb.value() >= out_sb.maximum() - 4
        self._in_list.clear()
        self._out_list.clear()
        for r in self._records_cache:
            ts = datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S.%f")[:-3]
            text = f"[{ts}]  {r['kind']}  seq={r['seq']}"
            if r["direction"] == "incoming":
                self._in_list.addItem(text)
            else:
                self._out_list.addItem(text)
        if in_at_bottom:
            self._in_list.scrollToBottom()
        if out_at_bottom:
            self._out_list.scrollToBottom()

    def _show_detail(self, item) -> None:
        text = item.text()
        for r in self._records_cache:
            ts = datetime.fromtimestamp(r["ts"]).strftime("%H:%M:%S.%f")[:-3]
            if f"[{ts}]" in text and r["kind"] in text:
                self._detail.setPlainText(json.dumps(r, indent=2, default=str))
                return

    # ------------------------------------------------------------------
    # Debug actions
    # ------------------------------------------------------------------

    def _send_ping(self) -> None:
        if self._send_fn:
            msg = {"op": "ping", "nonce": f"debug-{int(time.time())}", "ts": time.time()}
            self._send_fn(msg)
            self._recorder.record("outgoing", msg)

    def _send_hello(self) -> None:
        if self._send_fn:
            msg = {
                "op": "hello",
                "client_name": "IpcMonitorDialog",
                "client_version": "debug",
                "protocol_version": 1,
                "ts": time.time(),
            }
            self._send_fn(msg)
            self._recorder.record("outgoing", msg)

    def _send_heartbeat(self) -> None:
        if self._send_fn:
            msg = {"op": "heartbeat", "ts": time.time()}
            self._send_fn(msg)
            self._recorder.record("outgoing", msg)

    def _clear(self) -> None:
        self._recorder.clear()
        self._in_list.clear()
        self._out_list.clear()
        self._detail.clear()

    # ------------------------------------------------------------------

    def closeEvent(self, event) -> None:
        self._timer.stop()
        super().closeEvent(event)
