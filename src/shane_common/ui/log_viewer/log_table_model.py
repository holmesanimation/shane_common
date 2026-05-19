# shane_common/ui/log_viewer/log_table_model.py
"""
Generic LogTableModel and LogFilterProxyModel.

LogTableModel: QAbstractTableModel backed by an append-only list[LogRow].
LogFilterProxyModel: QSortFilterProxyModel implementing severity / type /
instrument filtering.

Trading-specific type constants (TYPE_INSTRUMENTS, TYPE_ORDERS, etc.) and
their classifier have been removed.  Concrete apps supply their own
``type_classifier`` and ``type_labels`` at construction time.
"""
from __future__ import annotations

import json
import math
from collections import deque  # noqa: F401 — re-exported for compat
from datetime import datetime, timezone
from typing import Callable, Optional, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from PySide6 import QtCore, QtGui
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor

from shane_common.ui.log_viewer.log_row import LogRow


# ------------------------------------------------------------------ #
# Column definitions
# ------------------------------------------------------------------ #

COLUMNS: list[tuple[str, str]] = [
    ("timestamp", "UTC Time"),
    ("severity", "Level"),
    ("type_label", "Type"),
    ("instrument", "Instrument"),
    ("kind", "Kind"),
    ("message", "Message"),
    ("notes", "\U0001f4c4"),
]

COL_TIMESTAMP = 0
COL_SEVERITY = 1
COL_TYPE = 2
COL_INSTRUMENT = 3
COL_KIND = 4
COL_MESSAGE = 5
COL_NOTES = 6

# ------------------------------------------------------------------ #
# Custom data roles
# ------------------------------------------------------------------ #
LOG_ROW_ROLE: int = Qt.ItemDataRole.UserRole + 10
TS_EPOCH_ROLE: int = Qt.ItemDataRole.UserRole + 11
HAS_NOTE_ROLE: int = Qt.ItemDataRole.UserRole + 12

# ------------------------------------------------------------------ #
# Severity ordering (lower numeric = less severe)
# ------------------------------------------------------------------ #
SEVERITY_ORDER: dict[str, int] = {
    "TRACE": 0,
    "DEBUG": 1,
    "INFO": 2,
    "WARN": 3,
    "ERROR": 4,
}

# ------------------------------------------------------------------ #
# Generic type-filter category
# ------------------------------------------------------------------ #
TYPE_APP_WIDE = 1 << 0
TYPE_ALL = 0xFFFF

_DEFAULT_TYPE_LABELS: dict[int, str] = {TYPE_APP_WIDE: "App"}


def _default_classifier(row: LogRow) -> int:
    return TYPE_APP_WIDE


# ------------------------------------------------------------------ #
# Grey-future colour
# ------------------------------------------------------------------ #
_GREY_FUTURE = QtCore.Qt.GlobalColor.darkGray

# ------------------------------------------------------------------ #
# Severity foreground colours
# ------------------------------------------------------------------ #
_FG_INFO  = QColor("#4FC3F7")   # light blue
_FG_WARN  = QColor("#FFB74D")   # orange
_FG_ERROR = QColor("#FF5252")   # bright red

# ------------------------------------------------------------------ #
# Event-kind background colours
# ------------------------------------------------------------------ #
_BG_POS_OPENED = QColor(76,  175,  80, 40)   # faded green
_BG_POS_CLOSED = QColor(244,  67,  54, 40)   # faded red
_BG_ORDER      = QColor(255, 152,   0, 40)   # faded orange

_POS_OPENED_KINDS = frozenset({"position.opened"})
_POS_CLOSED_KINDS = frozenset({"position.closed"})
_ORDER_BG_KINDS   = frozenset({
    "orderplan.created", "order.submitted", "order.ack",
    "order.filled", "order.canceled",
})


_TZ_COLUMN_HEADERS: dict[str, str] = {
    "UTC": "UTC Time",
    "America/New_York": "NY Time",
    "local": "Local Time",
}


def _build_tz(name: str):
    """Return a tzinfo for an IANA timezone name; falls back to UTC."""
    if not name or name == "UTC":
        return timezone.utc
    if name == "local":
        return datetime.now().astimezone().tzinfo
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, KeyError):
        return timezone.utc


# ===================================================================== #
# LogTableModel
# ===================================================================== #

class LogTableModel(QtCore.QAbstractTableModel):
    """
    Append-only table model for LogRow objects.

    Parameters
    ----------
    type_classifier : callable, optional
        ``(row: LogRow) -> int`` mapping each row to a type bitmask bit.
        Defaults to ``lambda row: TYPE_APP_WIDE``.
    type_labels : dict, optional
        ``{int: str}`` labels for the Type column.
        Defaults to ``{TYPE_APP_WIDE: "App"}``.
    """

    MAX_ROWS: int = 50_000
    _MAX_ROWS_FOR_LIVE_RECOLOUR: int = 500

    def __init__(
        self,
        parent: QtCore.QObject | None = None,
        type_classifier: Callable[[LogRow], int] | None = None,
        type_labels: dict[int, str] | None = None,
    ) -> None:
        super().__init__(parent)
        self._type_classifier: Callable[[LogRow], int] = (
            type_classifier if type_classifier is not None else _default_classifier
        )
        self._type_labels: dict[int, str] = (
            type_labels if type_labels is not None else dict(_DEFAULT_TYPE_LABELS)
        )
        self._rows: list[LogRow] = []
        self._noted_ts: set[float] = set()
        self._current_ts: float = math.inf  # no grey-future by default
        self._grey_future_takes_precedence: bool = True
        self._display_tz_name: str = "UTC"
        self._display_tz = timezone.utc

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @property
    def current_ts(self) -> float:
        return self._current_ts

    @current_ts.setter
    def current_ts(self, value: float) -> None:
        if value == self._current_ts:
            return
        self._current_ts = value
        if self._rows and len(self._rows) <= self._MAX_ROWS_FOR_LIVE_RECOLOUR:
            top_left = self.index(0, 0)
            bottom_right = self.index(len(self._rows) - 1, len(COLUMNS) - 1)
            self.dataChanged.emit(
                top_left, bottom_right,
                [Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.BackgroundRole],
            )

    def set_grey_future_takes_precedence(self, value: bool) -> None:
        """Control whether grey-future styling overrides severity foreground colours."""
        self._grey_future_takes_precedence = value

    def set_display_tz(self, tz_name: str) -> None:
        """Set the IANA timezone name used to format timestamp cells."""
        name = tz_name if isinstance(tz_name, str) and tz_name else "UTC"
        if name == self._display_tz_name:
            return
        self._display_tz_name = name
        self._display_tz = _build_tz(name)
        self.headerDataChanged.emit(Qt.Orientation.Horizontal, COL_TIMESTAMP, COL_TIMESTAMP)
        if self._rows:
            top_left = self.index(0, COL_TIMESTAMP)
            bottom_right = self.index(len(self._rows) - 1, COL_TIMESTAMP)
            self.dataChanged.emit(
                top_left, bottom_right, [Qt.ItemDataRole.DisplayRole]
            )

    def append_row(self, row: LogRow) -> None:
        """Append a single LogRow (live streaming path)."""
        pos = len(self._rows)
        self.beginInsertRows(QtCore.QModelIndex(), pos, pos)
        self._rows.append(row)
        self.endInsertRows()
        self._enforce_max()

    def set_rows(self, rows: Sequence[LogRow]) -> None:
        """Bulk-replace all rows (replay seek path)."""
        self.beginResetModel()
        self._rows = list(rows)
        self.endResetModel()

    def row_at(self, row_idx: int) -> LogRow | None:
        if 0 <= row_idx < len(self._rows):
            return self._rows[row_idx]
        return None

    def mark_note(self, ts: float) -> None:
        """Mark *ts* as having an associated note and repaint the Notes column."""
        self._noted_ts.add(ts)
        for i, row in enumerate(self._rows):
            if row.ts == ts:
                idx = self.index(i, COL_NOTES)
                self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DisplayRole, HAS_NOTE_ROLE])

    def load_notes(self, ts_set) -> None:
        """Replace the noted-ts set in bulk (replay load path)."""
        self._noted_ts = set(ts_set)
        if self._rows:
            top = self.index(0, COL_NOTES)
            bot = self.index(len(self._rows) - 1, COL_NOTES)
            self.dataChanged.emit(top, bot, [Qt.ItemDataRole.DisplayRole, HAS_NOTE_ROLE])

    # ------------------------------------------------------------------ #
    # QAbstractTableModel interface
    # ------------------------------------------------------------------ #

    def rowCount(self, parent: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._rows)

    def columnCount(self, parent: QtCore.QModelIndex = QtCore.QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(COLUMNS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.FontRole and section == COL_NOTES:
            font = QtGui.QFont()
            font.setBold(True)
            return font
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if 0 <= section < len(COLUMNS):
            if section == COL_TIMESTAMP:
                return _TZ_COLUMN_HEADERS.get(self._display_tz_name, "Time")
            return COLUMNS[section][1]
        return None

    def data(self, index: QtCore.QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        row_idx = index.row()
        col_idx = index.column()

        if row_idx < 0 or row_idx >= len(self._rows):
            return None
        if col_idx < 0 or col_idx >= len(COLUMNS):
            return None

        row = self._rows[row_idx]

        # Custom roles
        if role == LOG_ROW_ROLE:
            return row
        if role == TS_EPOCH_ROLE:
            return row.ts
        if role == HAS_NOTE_ROLE:
            return row.ts in self._noted_ts

        # Severity foreground colouring
        if role == Qt.ItemDataRole.ForegroundRole:
            sev = row.severity
            if sev == "INFO":
                return _FG_INFO
            if sev == "WARN":
                return _FG_WARN
            if sev == "ERROR":
                return _FG_ERROR
            return None

        # Background colouring by event kind
        if role == Qt.ItemDataRole.BackgroundRole:
            kind = row.kind
            if kind in _POS_OPENED_KINDS:
                return _BG_POS_OPENED
            if kind in _POS_CLOSED_KINDS:
                return _BG_POS_CLOSED
            if kind in _ORDER_BG_KINDS:
                return _BG_ORDER
            return None

        # Center-align the Notes indicator column
        if role == Qt.ItemDataRole.TextAlignmentRole and col_idx == COL_NOTES:
            return Qt.AlignmentFlag.AlignCenter

        if role != Qt.ItemDataRole.DisplayRole:
            return None

        return self._format_cell(row, col_idx)

    # ------------------------------------------------------------------ #
    # Cell formatting
    # ------------------------------------------------------------------ #

    def _format_cell(self, row: LogRow, col: int) -> str:
        if col == COL_TIMESTAMP:
            dt = datetime.fromtimestamp(row.ts, tz=self._display_tz)
            return dt.strftime("%H:%M:%S.%f")[:-3]

        if col == COL_SEVERITY:
            return row.severity

        if col == COL_TYPE:
            return self._type_labels.get(self._type_classifier(row), "?")

        if col == COL_INSTRUMENT:
            return row.instrument or "\u2014"

        if col == COL_KIND:
            return row.kind

        if col == COL_MESSAGE:
            return row.message

        if col == COL_NOTES:
            return "\U0001f4c4" if row.ts in self._noted_ts else ""

        return ""

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _enforce_max(self) -> None:
        """Trim oldest rows when buffer exceeds MAX_ROWS."""
        overflow = len(self._rows) - self.MAX_ROWS
        if overflow <= 0:
            return
        self.beginRemoveRows(QtCore.QModelIndex(), 0, overflow - 1)
        del self._rows[:overflow]
        self.endRemoveRows()

    def format_row_as_text(self, row_idx: int) -> str:
        """Return a human-readable multi-field string for *row_idx*."""
        row = self.row_at(row_idx)
        if row is None:
            return ""
        col_width = max(len(header) for _, header in COLUMNS)
        lines: list[str] = []
        for col_idx, (_, header) in enumerate(COLUMNS):
            value = self._format_cell(row, col_idx)
            lines.append(f"{header:<{col_width}}  {value}")
        return "\n".join(lines)


# ===================================================================== #
# LogFilterProxyModel
# ===================================================================== #

class LogFilterProxyModel(QtCore.QSortFilterProxyModel):
    """
    Filter proxy for LogTableModel.

    Filter dimensions (AND-combined):
    1. Severity enabled set — hide rows below selected level.
    2. Type bitmask — bitmask of type-classifier bits to show.
    3. Instrument set — show only selected instruments (or all).
    4. Notes-only — hide rows without notes.

    Parameters
    ----------
    type_classifier : callable, optional
        Same callable used by the associated ``LogTableModel``.
        Defaults to ``lambda row: TYPE_APP_WIDE``.
    """

    def __init__(
        self,
        parent: QtCore.QObject | None = None,
        type_classifier: Callable[[LogRow], int] | None = None,
    ) -> None:
        super().__init__(parent)
        self._type_classifier: Callable[[LogRow], int] = (
            type_classifier if type_classifier is not None else _default_classifier
        )
        self._severity_enabled: set[str] = {"INFO", "WARN", "ERROR"}
        self._type_mask: int = TYPE_ALL
        self._instrument_filter: set[str] | None = None  # None = show all
        self._lock_to_active: bool = False
        self._active_instrument: str | None = None
        self._notes_only: bool = False

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def set_severity_enabled(self, level: str, enabled: bool) -> None:
        """Show or hide rows of the given severity level."""
        before = frozenset(self._severity_enabled)
        if enabled:
            self._severity_enabled.add(level)
        else:
            self._severity_enabled.discard(level)
        if frozenset(self._severity_enabled) != before:
            self.invalidateFilter()

    def set_type_mask(self, mask: int) -> None:
        """Set the type bitmask (OR of TYPE_* constants)."""
        if mask == self._type_mask:
            return
        self._type_mask = mask
        self.invalidateFilter()

    def set_instrument_filter(self, instruments: set[str] | None) -> None:
        """Show only *instruments* (or all if ``None``)."""
        if instruments == self._instrument_filter:
            return
        self._instrument_filter = instruments
        self.invalidateFilter()

    def set_lock_to_active(self, locked: bool) -> None:
        self._lock_to_active = locked
        if locked and self._active_instrument is not None:
            self.set_instrument_filter({self._active_instrument})
        elif not locked:
            self.set_instrument_filter(None)

    def on_instrument_activated(self, instrument: str) -> None:
        """Called when the active instrument changes."""
        self._active_instrument = instrument
        if self._lock_to_active:
            self.set_instrument_filter({instrument})

    def set_notes_only(self, enabled: bool) -> None:
        """When *enabled*, show only rows that have an associated note."""
        if enabled != self._notes_only:
            self._notes_only = enabled
            self.invalidateFilter()

    # ------------------------------------------------------------------ #
    # QSortFilterProxyModel override
    # ------------------------------------------------------------------ #

    def filterAcceptsRow(
        self, source_row: int, source_parent: QtCore.QModelIndex
    ) -> bool:
        model: LogTableModel = self.sourceModel()  # type: ignore[assignment]
        row = model.row_at(source_row)
        if row is None:
            return False

        # 1) Severity enabled set
        if row.severity not in self._severity_enabled:
            return False

        # 2) Type bitmask
        row_type = self._type_classifier(row)
        if not (self._type_mask & row_type):
            return False

        # 3) Instrument filter
        if self._instrument_filter is not None:
            if row.instrument is None:
                pass  # App-wide rows pass through
            elif row.instrument not in self._instrument_filter:
                return False

        # 4) Notes-only filter
        if self._notes_only and row.ts not in model._noted_ts:
            return False

        return True
