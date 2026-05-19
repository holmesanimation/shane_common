# shane_common/ui/notes/notes_browser_window.py
"""Notes Browser — standalone window for browsing, collecting, and correlating
notes across all owners.

Layout (three splitter columns):
    Left   — owner list
    Middle — notes table for selected owner
    Right  — detail panel split into Contents (top) and Collection (bottom)

Read-only browsing.  All writes go through NotesWriter, never this window.

Requires: shane_common[qt]  (PySide6>=6.5)
"""

from __future__ import annotations

import datetime
import traceback

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from shane_common.notes.notes_repository import NoteRow, NotesRepository
from shane_common.notes.collection_format import (
    build_collection_entry,
    build_collection_manifest_header,
    format_note_contents,
)

# ---------------------------------------------------------------------------
# Middle-table columns
# ---------------------------------------------------------------------------
_M_COL_LOCAL_TS  = 0
_M_COL_UTC_TIME  = 1
_M_COL_NOTE_TYPE = 2
_M_COL_TEXT      = 3
_M_NUM_BASE_COLS = 4


class NotesBrowserWindow(QtWidgets.QMainWindow):
    """Notes Browser window.

    Parameters
    ----------
    repository:
        ``NotesRepository`` instance used for discovery and lookups.
    parent:
        Optional Qt parent widget.
    """

    def __init__(
        self,
        repository: NotesRepository,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository = repository

        self._current_owner: str | None = None
        self._owner_rows: list[NoteRow] = []
        self._lock_time_ts: float | None = None
        self._writer: object | None = None

        self.setWindowTitle("Notes Browser")
        self.resize(1200, 700)

        self._build_ui()

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def set_repository(self, repository: NotesRepository) -> None:
        """Replace the repository (e.g. after a session starts)."""
        self._repository = repository
        self._refresh_owners()

    def set_writer(self, writer: object) -> None:
        """Inject a ``NotesWriter`` so correlation records can be persisted."""
        self._writer = writer

    def refresh(self) -> None:
        """Re-read disk and refresh the current view."""
        self._refresh_owners()
        if self._current_owner:
            self._load_owner(self._current_owner)

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QtWidgets.QWidget(self)
        self.setCentralWidget(central)
        root = QtWidgets.QHBoxLayout(central)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(0)

        splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal, central)
        root.addWidget(splitter)

        # ----------------------------------------------------------------
        # Left — owner list
        # ----------------------------------------------------------------
        left_panel = QtWidgets.QWidget(splitter)
        left_layout = QtWidgets.QVBoxLayout(left_panel)
        left_layout.setContentsMargins(4, 4, 4, 4)
        left_layout.setSpacing(4)

        lbl_owners = QtWidgets.QLabel("Owners", left_panel)
        f = lbl_owners.font()
        f.setBold(True)
        lbl_owners.setFont(f)
        left_layout.addWidget(lbl_owners)

        self._owner_list = QtWidgets.QListWidget(left_panel)
        self._owner_list.currentItemChanged.connect(self._on_owner_selected)
        left_layout.addWidget(self._owner_list, stretch=1)

        btn_refresh = QtWidgets.QPushButton("Refresh", left_panel)
        btn_refresh.clicked.connect(self.refresh)
        left_layout.addWidget(btn_refresh)

        splitter.addWidget(left_panel)

        # ----------------------------------------------------------------
        # Middle — notes table
        # ----------------------------------------------------------------
        middle_panel = QtWidgets.QWidget(splitter)
        middle_layout = QtWidgets.QVBoxLayout(middle_panel)
        middle_layout.setContentsMargins(4, 4, 4, 4)
        middle_layout.setSpacing(4)

        top_bar = QtWidgets.QHBoxLayout()
        self._lbl_middle_title = QtWidgets.QLabel("", middle_panel)
        _f = self._lbl_middle_title.font()
        _f.setBold(True)
        self._lbl_middle_title.setFont(_f)
        top_bar.addWidget(self._lbl_middle_title, stretch=1)

        self._btn_lock_time = QtWidgets.QPushButton("Lock Time", middle_panel)
        self._btn_lock_time.setCheckable(True)
        self._btn_lock_time.setChecked(False)
        self._btn_lock_time.setStyleSheet(
            "QPushButton:checked { border: 1px solid #f59e0b;"
            " color: #f59e0b; background: #2a1f00; }"
        )
        self._btn_lock_time.clicked.connect(self._on_lock_time_clicked)
        top_bar.addWidget(self._btn_lock_time)

        self._btn_browse_folder = QtWidgets.QToolButton(middle_panel)
        self._btn_browse_folder.setIcon(
            self.style().standardIcon(QtWidgets.QStyle.StandardPixmap.SP_DirOpenIcon)
        )
        self._btn_browse_folder.setToolTip("Open notes folder in Explorer")
        self._btn_browse_folder.clicked.connect(self._on_browse_folder)
        top_bar.addWidget(self._btn_browse_folder)

        middle_layout.addLayout(top_bar)

        self._middle_table = QtWidgets.QTableWidget(0, _M_NUM_BASE_COLS, middle_panel)
        self._middle_table.setHorizontalHeaderLabels(
            ["Local TS", "UTC Time", "Type", "Text"]
        )
        self._middle_table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self._middle_table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        self._middle_table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self._middle_table.setAlternatingRowColors(False)
        self._middle_table.verticalHeader().setVisible(False)
        hdr = self._middle_table.horizontalHeader()
        hdr.setSectionResizeMode(_M_COL_LOCAL_TS,  QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(_M_COL_UTC_TIME,  QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(_M_COL_NOTE_TYPE, QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(_M_COL_TEXT,      QtWidgets.QHeaderView.ResizeMode.Stretch)
        self._middle_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._middle_table.customContextMenuRequested.connect(self._on_middle_context_menu)
        self._middle_table.selectionModel().currentRowChanged.connect(self._on_middle_row_changed)
        middle_layout.addWidget(self._middle_table, stretch=1)

        splitter.addWidget(middle_panel)

        # ----------------------------------------------------------------
        # Right — detail panel (Contents / Collection)
        # ----------------------------------------------------------------
        right_panel = QtWidgets.QWidget(splitter)
        right_layout = QtWidgets.QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 4, 4, 4)
        right_layout.setSpacing(4)

        right_splitter = QtWidgets.QSplitter(Qt.Orientation.Vertical, right_panel)
        right_layout.addWidget(right_splitter, stretch=1)

        # Contents area
        contents_widget = QtWidgets.QWidget(right_splitter)
        cv = QtWidgets.QVBoxLayout(contents_widget)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.setSpacing(2)

        contents_bar = QtWidgets.QHBoxLayout()
        contents_bar.addWidget(QtWidgets.QLabel("Contents", contents_widget))
        contents_bar.addStretch(1)
        btn_copy_to_col = QtWidgets.QToolButton(contents_widget)
        btn_copy_to_col.setText("+ Collection")
        btn_copy_to_col.setToolTip("Append this note to the Collection panel")
        btn_copy_to_col.clicked.connect(self._on_copy_to_collection)
        contents_bar.addWidget(btn_copy_to_col)
        cv.addLayout(contents_bar)

        self._contents_edit = QtWidgets.QTextEdit(contents_widget)
        self._contents_edit.setReadOnly(True)
        cv.addWidget(self._contents_edit, stretch=1)

        right_splitter.addWidget(contents_widget)

        # Collection area
        collection_widget = QtWidgets.QWidget(right_splitter)
        clv = QtWidgets.QVBoxLayout(collection_widget)
        clv.setContentsMargins(0, 0, 0, 0)
        clv.setSpacing(2)

        coll_bar = QtWidgets.QHBoxLayout()
        coll_bar.addWidget(QtWidgets.QLabel("Collection", collection_widget))
        coll_bar.addStretch(1)
        btn_copy_coll = QtWidgets.QToolButton(collection_widget)
        btn_copy_coll.setText("Copy")
        btn_copy_coll.setToolTip("Copy collection text to clipboard")
        btn_copy_coll.clicked.connect(self._on_copy_collection_clipboard)
        coll_bar.addWidget(btn_copy_coll)
        btn_clear_coll = QtWidgets.QToolButton(collection_widget)
        btn_clear_coll.setText("Clear")
        btn_clear_coll.setToolTip("Clear the collection")
        btn_clear_coll.clicked.connect(self._collection_edit_clear)
        coll_bar.addWidget(btn_clear_coll)
        clv.addLayout(coll_bar)

        self._collection_edit = QtWidgets.QTextEdit(collection_widget)
        clv.addWidget(self._collection_edit, stretch=1)

        right_splitter.addWidget(collection_widget)
        right_splitter.setSizes([400, 250])

        splitter.addWidget(right_panel)
        splitter.setSizes([180, 600, 420])

        self._refresh_owners()

    # ------------------------------------------------------------------
    # Owner list
    # ------------------------------------------------------------------

    def _refresh_owners(self) -> None:
        try:
            owners = self._repository.list_owners()
        except Exception:
            traceback.print_exc()
            owners = []

        prev_key = self._current_owner
        self._owner_list.blockSignals(True)
        self._owner_list.clear()
        for key in owners:
            item = QtWidgets.QListWidgetItem(key)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self._owner_list.addItem(item)
        self._owner_list.blockSignals(False)

        if prev_key:
            for i in range(self._owner_list.count()):
                item = self._owner_list.item(i)
                if item and item.data(Qt.ItemDataRole.UserRole) == prev_key:
                    self._owner_list.setCurrentRow(i)
                    return
        if self._owner_list.count() > 0 and not prev_key:
            self._owner_list.setCurrentRow(0)

    def _on_owner_selected(
        self,
        current: QtWidgets.QListWidgetItem | None,
        previous: QtWidgets.QListWidgetItem | None,
    ) -> None:
        if current is None:
            self._current_owner = None
            self._owner_rows = []
            self._middle_table.setRowCount(0)
            self._contents_edit.clear()
            return
        key = current.data(Qt.ItemDataRole.UserRole)
        self._load_owner(key)

    # ------------------------------------------------------------------
    # Middle table
    # ------------------------------------------------------------------

    def _load_owner(self, owner_key: str) -> None:
        self._current_owner = owner_key
        self._lbl_middle_title.setText(owner_key)

        try:
            all_rows = self._repository.rows_for_owner(owner_key)
        except Exception:
            traceback.print_exc()
            all_rows = []

        by_note: dict[str, NoteRow] = {}
        for r in all_rows:
            key = r.note_id or r.revision_id or str(id(r))
            existing = by_note.get(key)
            if existing is None or (r.revision_num or 0) > (existing.revision_num or 0):
                by_note[key] = r

        self._owner_rows = sorted(by_note.values(), key=lambda r: r.ts or 0.0)
        self._rebuild_middle_table()

        if self._btn_lock_time.isChecked() and self._lock_time_ts is not None:
            self._select_nearest_locked_row()

    def _rebuild_middle_table(self) -> None:
        self._middle_table.setRowCount(0)
        for row_idx, note in enumerate(self._owner_rows):
            self._middle_table.insertRow(row_idx)
            self._set_middle_row(row_idx, note)

    def _set_middle_row(self, row_idx: int, note: NoteRow) -> None:
        local_str = "\u2014"
        utc_str = "\u2014"
        if note.ts:
            try:
                dt_utc = datetime.datetime.utcfromtimestamp(note.ts).replace(
                    tzinfo=datetime.timezone.utc
                )
                dt_local = dt_utc.astimezone()
                local_str = dt_local.strftime("%Y-%m-%d %H:%M:%S")
                utc_str   = dt_utc.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                pass

        self._middle_table.setItem(row_idx, _M_COL_LOCAL_TS, _cell(local_str))
        self._middle_table.setItem(row_idx, _M_COL_UTC_TIME, _cell(utc_str))
        self._middle_table.setItem(row_idx, _M_COL_NOTE_TYPE, _cell(note.note_type or ""))
        text_preview = (note.text or "").replace("\n", " ")[:80]
        self._middle_table.setItem(row_idx, _M_COL_TEXT, _cell(text_preview))

    # ------------------------------------------------------------------
    # Lock Time
    # ------------------------------------------------------------------

    def _on_browse_folder(self) -> None:
        """Open the notes root directory in the OS file explorer."""
        import subprocess
        folder = self._repository._root
        folder.mkdir(parents=True, exist_ok=True)
        subprocess.Popen(["explorer", str(folder)])

    def _on_lock_time_clicked(self, checked: bool) -> None:
        if not checked:
            self._lock_time_ts = None
            self._btn_lock_time.setText("Lock Time")
            self._clear_nearest_highlight()
            return

        sel_row = self._middle_table.currentRow()
        ts: float | None = None
        if 0 <= sel_row < len(self._owner_rows):
            ts = self._owner_rows[sel_row].ts
        if ts is None:
            self._btn_lock_time.setChecked(False)
            return

        self._lock_time_ts = ts
        try:
            dt = datetime.datetime.utcfromtimestamp(ts).replace(
                tzinfo=datetime.timezone.utc
            ).astimezone()
            label = dt.strftime("%H:%M:%S")
        except Exception:
            label = str(ts)
        self._btn_lock_time.setText(f"{label} locked")
        self._highlight_nearest_row(sel_row)

    def _select_nearest_locked_row(self) -> None:
        if self._lock_time_ts is None or not self._owner_rows:
            return
        nearest = _nearest_row_index(self._owner_rows, self._lock_time_ts)
        self._middle_table.setCurrentRow(nearest)
        self._highlight_nearest_row(nearest)
        self._middle_table.scrollToItem(
            self._middle_table.item(nearest, 0),
            QtWidgets.QAbstractItemView.ScrollHint.PositionAtCenter,
        )

    def _highlight_nearest_row(self, row_idx: int) -> None:
        self._clear_nearest_highlight()
        for col in range(self._middle_table.columnCount()):
            item = self._middle_table.item(row_idx, col)
            if item:
                item.setBackground(QtGui.QBrush(QtGui.QColor("#2a1f00")))
                item.setForeground(QtGui.QBrush(QtGui.QColor("#f59e0b")))

    def _clear_nearest_highlight(self) -> None:
        for r in range(self._middle_table.rowCount()):
            for c in range(self._middle_table.columnCount()):
                item = self._middle_table.item(r, c)
                if item:
                    item.setBackground(QtGui.QBrush())
                    item.setForeground(QtGui.QBrush())

    # ------------------------------------------------------------------
    # Row selection → Contents panel
    # ------------------------------------------------------------------

    def _on_middle_row_changed(self, current: QtCore.QModelIndex, previous: QtCore.QModelIndex) -> None:
        current_row = current.row() if current.isValid() else -1
        if current_row < 0 or current_row >= len(self._owner_rows):
            self._contents_edit.clear()
            return
        note = self._owner_rows[current_row]
        self._contents_edit.setPlainText(format_note_contents(note))

    # ------------------------------------------------------------------
    # Context menu
    # ------------------------------------------------------------------

    def _on_middle_context_menu(self, pos: QtCore.QPoint) -> None:
        row = self._middle_table.rowAt(pos.y())
        if row < 0:
            return
        menu = QtWidgets.QMenu(self)
        act_copy = menu.addAction("Copy row")
        act_copy.triggered.connect(lambda: self._copy_middle_row(row))

        selected_rows = sorted({idx.row() for idx in self._middle_table.selectedIndexes()})
        if len(selected_rows) >= 2:
            menu.addSeparator()
            act_correlate = menu.addAction("Create Correlation\u2026")
            act_correlate.triggered.connect(lambda: self._on_create_correlation(selected_rows))

        menu.exec(self._middle_table.viewport().mapToGlobal(pos))

    def _copy_middle_row(self, row_idx: int) -> None:
        if row_idx < 0 or row_idx >= len(self._owner_rows):
            return
        parts = []
        for col in range(self._middle_table.columnCount()):
            item = self._middle_table.item(row_idx, col)
            parts.append(item.text() if item else "")
        QtWidgets.QApplication.clipboard().setText("\t".join(parts))

    # ------------------------------------------------------------------
    # Correlation creation
    # ------------------------------------------------------------------

    def _on_create_correlation(self, row_indices: list[int]) -> None:
        notes = [
            self._owner_rows[i] for i in row_indices
            if 0 <= i < len(self._owner_rows)
        ]
        note_ids = [n.note_id for n in notes if n.note_id]
        if len(note_ids) < 2:
            QtWidgets.QMessageBox.information(
                self,
                "Create Correlation",
                "Select at least two notes with stable IDs to create a correlation.",
            )
            return

        dlg = _CorrelationDialog(note_ids=note_ids, parent=self)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        hypothesis = dlg.hypothesis()
        relation_type = dlg.relation_type()
        row_ids = [n.row_id for n in notes if n.row_id]

        writer = self._writer
        if writer is not None:
            try:
                from shane_common.notes.notes_writer import NoteCorrelation, make_correlation_id
                import time as _t
                correlation = NoteCorrelation(
                    correlation_id=make_correlation_id(),
                    note_ids=note_ids,
                    row_ids=row_ids,
                    time_window="",
                    relation_type=relation_type,
                    hypothesis=hypothesis,
                    tags=[],
                    created_ts=_t.time(),
                )
                writer.commit_correlation(correlation)
                QtWidgets.QMessageBox.information(
                    self,
                    "Correlation created",
                    f"Correlation {correlation.correlation_id} persisted.",
                )
            except Exception:
                traceback.print_exc()
        else:
            import json as _json
            snippet = _json.dumps(
                {
                    "note_ids": note_ids,
                    "row_ids": row_ids,
                    "relation_type": relation_type,
                    "hypothesis": hypothesis,
                },
                indent=2,
            )
            QtWidgets.QMessageBox.information(
                self,
                "Correlation (no writer)",
                f"No writer injected. Correlation record:\n\n{snippet}",
            )

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------

    def _on_copy_to_collection(self) -> None:
        sel_row = self._middle_table.currentRow()
        if sel_row < 0 or sel_row >= len(self._owner_rows):
            return
        note = self._owner_rows[sel_row]
        entry_text = build_collection_entry(note)
        existing = self._collection_edit.toPlainText()
        if existing.strip():
            self._collection_edit.setPlainText(existing + "\n\n" + entry_text)
        else:
            header = build_collection_manifest_header([note])
            self._collection_edit.setPlainText(header + "\n\n" + entry_text)

    def _on_copy_collection_clipboard(self) -> None:
        text = self._collection_edit.toPlainText()
        if text.strip():
            if "USER QUERY:" not in text:
                text = text + "\n\nUSER QUERY:\n"
            QtWidgets.QApplication.clipboard().setText(text)

    def _collection_edit_clear(self) -> None:
        self._collection_edit.clear()


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _nearest_row_index(rows: list[NoteRow], target_ts: float) -> int:
    best_idx = 0
    best_diff = float("inf")
    for i, row in enumerate(rows):
        ts = row.ts
        if ts is None:
            continue
        diff = abs(ts - target_ts)
        if diff < best_diff:
            best_diff = diff
            best_idx = i
    return best_idx


def _cell(text: str) -> QtWidgets.QTableWidgetItem:
    item = QtWidgets.QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


# ---------------------------------------------------------------------------
# Correlation creation dialog
# ---------------------------------------------------------------------------

class _CorrelationDialog(QtWidgets.QDialog):
    """Minimal dialog for capturing hypothesis and relation type."""

    _RELATION_TYPES = ["cause-effect", "concurrent", "sequence", "contrast", "other"]

    def __init__(
        self,
        note_ids: list[str],
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Create Correlation")
        self.setMinimumWidth(480)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(8)

        info = QtWidgets.QLabel(
            f"Linking {len(note_ids)} note(s):\n"
            + "\n".join(f"  \u2022 {nid}" for nid in note_ids[:5])
            + ("\n  \u2026" if len(note_ids) > 5 else "")
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        type_row = QtWidgets.QHBoxLayout()
        type_row.addWidget(QtWidgets.QLabel("Relation:"))
        self._type_combo = QtWidgets.QComboBox()
        for rt in self._RELATION_TYPES:
            self._type_combo.addItem(rt)
        type_row.addWidget(self._type_combo, stretch=1)
        layout.addLayout(type_row)

        layout.addWidget(QtWidgets.QLabel("Hypothesis / notes:"))
        self._hypothesis_edit = QtWidgets.QTextEdit()
        self._hypothesis_edit.setFixedHeight(120)
        layout.addWidget(self._hypothesis_edit)

        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def hypothesis(self) -> str:
        return self._hypothesis_edit.toPlainText().strip()

    def relation_type(self) -> str:
        return self._type_combo.currentText()
