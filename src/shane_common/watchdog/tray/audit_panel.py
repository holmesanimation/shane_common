"""
AuditPanel — generic scrollable JSONL audit table widget.

Displays the last *tail_n* records from an append-only JSONL file.
Columns rendered: timestamp, event/note (all remaining fields joined).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from PySide6 import QtCore, QtWidgets
from PySide6.QtCore import Qt

from ..audit import AppendOnlyAuditLog

_TS_FIELDS = ("ts_wall_utc", "ts", "timestamp")
_EVENT_FIELDS = ("event", "note", "transition", "action")


def _extract_ts(record: dict) -> Optional[float]:
    for key in _TS_FIELDS:
        if key in record:
            try:
                return float(record[key])
            except (TypeError, ValueError):
                pass
    return None


def _extract_summary(record: dict) -> str:
    """Build a human-readable summary from the most informative fields."""
    for key in _EVENT_FIELDS:
        if key in record:
            val = record[key]
            # Include a few extra fields for context
            extras = {
                k: v for k, v in record.items()
                if k not in _TS_FIELDS and k not in _EVENT_FIELDS and k != "app_id"
            }
            if extras:
                snippet = " | ".join(f"{k}={v}" for k, v in list(extras.items())[:3])
                return f"{val}  [{snippet}]"
            return str(val)
    # Fall back to full record sans ts fields
    condensed = {k: v for k, v in record.items() if k not in _TS_FIELDS}
    return json.dumps(condensed, ensure_ascii=False)


class AuditPanel(QtWidgets.QWidget):
    """
    A scrollable table showing the last *tail_n* records from *audit_path*.

    Parameters
    ----------
    audit_path:
        Path to the JSONL file to read.
    tail_n:
        Maximum number of records to display (most recent).
    """

    def __init__(
        self,
        audit_path: Path,
        tail_n: int = 50,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._audit_log = AppendOnlyAuditLog(audit_path)
        self._tail_n = tail_n
        self._build_ui()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def refresh(self) -> None:
        """Re-read the audit file and update the table."""
        records = self._audit_log.tail(self._tail_n)
        self._table.setRowCount(0)
        for record in reversed(records):  # most recent at top
            row = self._table.rowCount()
            self._table.insertRow(row)

            ts = _extract_ts(record)
            ts_str = (
                time.strftime("%H:%M:%S", time.localtime(ts))
                if ts is not None
                else "—"
            )
            summary = _extract_summary(record)
            app_id = str(record.get("app_id", ""))

            self._table.setItem(row, 0, QtWidgets.QTableWidgetItem(ts_str))
            self._table.setItem(row, 1, QtWidgets.QTableWidgetItem(app_id))
            self._table.setItem(row, 2, QtWidgets.QTableWidgetItem(summary))

        self._table.resizeColumnsToContents()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        label = QtWidgets.QLabel("Recent Events")
        label.setStyleSheet("font-weight: bold; font-size: 12px;")
        layout.addWidget(label)

        self._table = QtWidgets.QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Time", "App", "Event"])
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(
            QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        layout.addWidget(self._table)
