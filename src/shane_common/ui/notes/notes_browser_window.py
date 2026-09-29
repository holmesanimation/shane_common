# shane_common/ui/notes/notes_browser_window.py
"""Notes Browser — standalone window for browsing, collecting, and correlating
notes across all owners.

Layout (four splitter columns):
    Owners   — owner list, "New Owner"
    Subjects — ALL / UNSET / subjects for the selected owner, "New Subject"
    Middle   — notes table for the selected owner and subject
    Right    — detail panel split into Contents (top) and Collection (bottom)

Browsing reads through the repository.  Edits, deletes and new owners/subjects
are appended through ``NotesWriter`` / ``NotesRegistry`` — earlier revisions of
a note are never rewritten.

Requires: shane_common[qt]  (PySide6>=6.5)
"""

from __future__ import annotations

import datetime
import traceback

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt

from shane_common.notes.notes_repository import NoteRow, NotesRepository
from shane_common.notes.notes_writer import OP_DELETE, UNSET_SUBJECT
from shane_common.notes.registry import (
    ALL_SUBJECTS_LABEL,
    NotesRegistry,
    get_default_registry,
)
from shane_common.notes.revisions import commit_revision
from shane_common.notes.collection_format import (
    build_collection_entry,
    build_collection_manifest_header,
    format_note_contents,
)
from shane_common.ui.notes.note_dialog import EditNoteDialog
from shane_common.ui.notes.subject_picker import NewOwnerDialog, NewSubjectDialog

# ---------------------------------------------------------------------------
# Middle-table columns
# ---------------------------------------------------------------------------
_M_COL_LOCAL_TS  = 0
_M_COL_UTC_TIME  = 1
_M_COL_NOTE_TYPE = 2
_M_COL_TEXT      = 3
_M_NUM_BASE_COLS = 4

_UNTAGGED_SENTINEL = "(untagged)"

# Subject-list filter values.  Real subjects use their registry id; UNSET is the
# stored value for notes without a subject.  ALL is list-only (never stored).
_FILTER_ALL = "\0all"
_FILTER_UNSET = UNSET_SUBJECT


class NotesBrowserWindow(QtWidgets.QMainWindow):
    """Notes Browser window.

    Parameters
    ----------
    repository:
        ``NotesRepository`` instance used for discovery and lookups.
    registry:
        ``NotesRegistry`` of owners and subjects.  Defaults to the
        application-wide registry; without one the owner/subject creation
        buttons are disabled and subjects show by raw id.
    parent:
        Optional Qt parent widget.
    """

    def __init__(
        self,
        repository: NotesRepository,
        parent: QtWidgets.QWidget | None = None,
        *,
        registry: NotesRegistry | None = None,
    ) -> None:
        super().__init__(parent)
        self._repository = repository
        self._registry = registry if registry is not None else get_default_registry()

        self._current_owner: str | None = None
        self._current_subject: str = _FILTER_ALL
        self._owner_rows: list[NoteRow] = []
        self._visible_rows: list[NoteRow] = []
        self._active_tag_filter: set[str] | None = None
        self._lock_time_ts: float | None = None
        self._writer = None  # NotesWriter, injected via set_writer()

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
        """Inject a ``NotesWriter`` used to persist correlations, edits and deletes.

        Edits and deletes are filed under the note's own owner via
        ``writer.for_owner(...)``, so any writer sharing the desired store will do.
        """
        self._writer = writer

    def set_registry(self, registry: NotesRegistry | None) -> None:
        """Replace the owner/subject registry."""
        self._registry = registry
        self._update_create_buttons()
        self.refresh()

    def refresh(self) -> None:
        """Re-read the database and refresh the current view."""
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

        self._btn_new_owner = QtWidgets.QPushButton("New Owner", left_panel)
        self._btn_new_owner.clicked.connect(self._on_new_owner)
        left_layout.addWidget(self._btn_new_owner)

        btn_refresh = QtWidgets.QPushButton("Refresh", left_panel)
        btn_refresh.clicked.connect(self.refresh)
        left_layout.addWidget(btn_refresh)

        splitter.addWidget(left_panel)

        # ----------------------------------------------------------------
        # Subjects list (between owners and the notes table)
        # ----------------------------------------------------------------
        subjects_panel = QtWidgets.QWidget(splitter)
        subjects_layout = QtWidgets.QVBoxLayout(subjects_panel)
        subjects_layout.setContentsMargins(4, 4, 4, 4)
        subjects_layout.setSpacing(4)

        lbl_subjects = QtWidgets.QLabel("Subjects", subjects_panel)
        lbl_subjects.setFont(f)
        subjects_layout.addWidget(lbl_subjects)

        self._subject_list = QtWidgets.QListWidget(subjects_panel)
        self._subject_list.currentItemChanged.connect(self._on_subject_selected)
        subjects_layout.addWidget(self._subject_list, stretch=1)

        self._btn_new_subject = QtWidgets.QPushButton("New Subject", subjects_panel)
        self._btn_new_subject.clicked.connect(self._on_new_subject)
        subjects_layout.addWidget(self._btn_new_subject)

        splitter.addWidget(subjects_panel)
        self._update_create_buttons()

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
        self._btn_browse_folder.setToolTip("Open the notes database folder in Explorer")
        self._btn_browse_folder.clicked.connect(self._on_browse_folder)
        top_bar.addWidget(self._btn_browse_folder)

        middle_layout.addLayout(top_bar)

        self._middle_table = QtWidgets.QTableWidget(0, _M_NUM_BASE_COLS, middle_panel)
        self._middle_table.setHorizontalHeaderLabels(
            ["Time", "UTC Time", "Tags", "Title"]
        )
        self._middle_table.setColumnHidden(_M_COL_UTC_TIME, True)
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
        hdr.sectionClicked.connect(self._on_header_section_clicked)
        self._middle_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._middle_table.customContextMenuRequested.connect(self._on_middle_context_menu)
        # Tell the app-wide notes context-menu filter (Add Note / Browse Notes)
        # to leave this widget's own menu alone.
        self._middle_table.setProperty("ownContextMenu", True)
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
        splitter.setSizes([160, 160, 520, 380])

        self._refresh_owners()

    def _update_create_buttons(self) -> None:
        enabled = self._registry is not None
        tip = "" if enabled else "No owner/subject registry is configured."
        for btn in (self._btn_new_owner, self._btn_new_subject):
            btn.setEnabled(enabled)
            btn.setToolTip(tip)

    # ------------------------------------------------------------------
    # Owner list
    # ------------------------------------------------------------------

    def _stored_owner_keys(self) -> list[str]:
        """Owner keys that have notes (also listed when no registry is configured)."""
        try:
            return list(self._repository.list_owners())
        except Exception:
            traceback.print_exc()
            return []

    def _owner_label(self, key: str) -> str:
        return self._registry.owner_label(key) if self._registry is not None else key

    def _subject_label(self, subject_id: str) -> str:
        return self._registry.subject_label(subject_id) if self._registry is not None else subject_id

    def _refresh_owners(self) -> None:
        # Registry owners (built-in + user-created, even with no notes yet) plus
        # any owner that has notes.
        keys = {k: None for k in self._stored_owner_keys()}
        if self._registry is not None:
            keys.update({o.key: None for o in self._registry.owners()})
        owners = sorted(keys, key=lambda k: self._owner_label(k).casefold())

        prev_key = self._current_owner
        self._owner_list.blockSignals(True)
        self._owner_list.clear()
        for key in owners:
            item = QtWidgets.QListWidgetItem(self._owner_label(key))
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
            self._subject_list.clear()
            self._middle_table.setRowCount(0)
            self._contents_edit.clear()
            return
        key = current.data(Qt.ItemDataRole.UserRole)
        self._load_owner(key)

    # ------------------------------------------------------------------
    # Subject list
    # ------------------------------------------------------------------

    def _refresh_subjects(self) -> None:
        """Rebuild the subject list: ALL, UNSET, then the owner's subjects."""
        owner = self._current_owner
        entries: list[tuple[str, str]] = [
            (ALL_SUBJECTS_LABEL, _FILTER_ALL),
            (UNSET_SUBJECT, _FILTER_UNSET),
        ]
        if owner is not None and self._registry is not None:
            for s in self._registry.subjects_for_owner(owner):
                entries.append((s.label, s.id))

        self._subject_list.blockSignals(True)
        self._subject_list.clear()
        for label, value in entries:
            item = QtWidgets.QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, value)
            self._subject_list.addItem(item)
        row = next(
            (i for i, (_, value) in enumerate(entries) if value == self._current_subject), 0
        )
        self._current_subject = entries[row][1]
        self._subject_list.setCurrentRow(row)
        self._subject_list.blockSignals(False)

    def _on_subject_selected(
        self,
        current: QtWidgets.QListWidgetItem | None,
        previous: QtWidgets.QListWidgetItem | None,
    ) -> None:
        if current is None:
            return
        self._current_subject = current.data(Qt.ItemDataRole.UserRole)
        self._rebuild_middle_table()

    def _subject_rows(self) -> list[NoteRow]:
        """Owner rows narrowed by the selected subject (before the tag filter)."""
        if self._current_subject == _FILTER_ALL:
            return list(self._owner_rows)
        return [n for n in self._owner_rows if n.subject == self._current_subject]

    # ------------------------------------------------------------------
    # Middle table
    # ------------------------------------------------------------------

    def _load_owner(self, owner_key: str) -> None:
        if owner_key != self._current_owner:
            self._current_subject = _FILTER_ALL
        self._current_owner = owner_key
        self._lbl_middle_title.setText(self._owner_label(owner_key))
        self._active_tag_filter = None
        self._middle_table.horizontalHeaderItem(_M_COL_NOTE_TYPE).setText("Tags")

        # Latest revision per note (deleted notes hidden), newest first.
        try:
            self._owner_rows = self._repository.latest_notes_for_owner(
                owner_key, newest_first=True
            )
        except Exception:
            traceback.print_exc()
            self._owner_rows = []
        self._refresh_subjects()
        self._rebuild_middle_table()

        if self._btn_lock_time.isChecked() and self._lock_time_ts is not None:
            self._select_nearest_locked_row()

    def _rebuild_middle_table(self) -> None:
        subject_rows = self._subject_rows()
        if self._active_tag_filter is None:
            self._visible_rows = subject_rows
        else:
            self._visible_rows = []
            for note in subject_rows:
                tags = note.context.get("tags", []) if isinstance(note.context, dict) else []
                if tags:
                    if any(t in self._active_tag_filter for t in tags):
                        self._visible_rows.append(note)
                else:
                    if _UNTAGGED_SENTINEL in self._active_tag_filter:
                        self._visible_rows.append(note)
        self._middle_table.setRowCount(0)
        for row_idx, note in enumerate(self._visible_rows):
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

        tags = note.context.get("tags", []) if isinstance(note.context, dict) else []
        tags_str = ", ".join(tags) if tags else "\u2014"
        self._middle_table.setItem(row_idx, _M_COL_LOCAL_TS, _cell(local_str))
        self._middle_table.setItem(row_idx, _M_COL_UTC_TIME, _cell(utc_str))
        self._middle_table.setItem(row_idx, _M_COL_NOTE_TYPE, _cell(tags_str))
        # Title column: the note's title when it has one, else a preview of its text.
        title = note.title or (note.text or "").replace("\n", " ")[:80]
        self._middle_table.setItem(row_idx, _M_COL_TEXT, _cell(title))

    # ------------------------------------------------------------------
    # Lock Time
    # ------------------------------------------------------------------

    def _on_header_section_clicked(self, logical_index: int) -> None:
        if logical_index != _M_COL_NOTE_TYPE:
            return
        hdr = self._middle_table.horizontalHeader()
        x = hdr.sectionViewportPosition(logical_index)
        pos = hdr.mapToGlobal(QtCore.QPoint(x, hdr.height()))

        unique_tags: set[str] = set()
        has_untagged = False
        for note in self._subject_rows():
            tags = note.context.get("tags", []) if isinstance(note.context, dict) else []
            if tags:
                unique_tags.update(tags)
            else:
                has_untagged = True

        popup = _TagFilterPopup(
            tags=sorted(unique_tags),
            has_untagged=has_untagged,
            active_filter=self._active_tag_filter,
            on_changed=self._apply_tag_filter,
        )
        popup.move(pos)
        popup.show()
        popup.raise_()

    def _apply_tag_filter(self, active_filter: "set[str] | None") -> None:
        self._active_tag_filter = active_filter
        hdr_item = self._middle_table.horizontalHeaderItem(_M_COL_NOTE_TYPE)
        if hdr_item:
            hdr_item.setText("Tags" if active_filter is None else "Tags \u25cf")
        self._rebuild_middle_table()

    def _on_browse_folder(self) -> None:
        """Open the folder holding the notes database in the OS file explorer."""
        import subprocess
        folder = self._repository.location
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
        if 0 <= sel_row < len(self._visible_rows):
            ts = self._visible_rows[sel_row].ts
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
        if self._lock_time_ts is None or not self._visible_rows:
            return
        nearest = _nearest_row_index(self._visible_rows, self._lock_time_ts)
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
        if current_row < 0 or current_row >= len(self._visible_rows):
            self._contents_edit.clear()
            return
        note = self._visible_rows[current_row]
        self._contents_edit.setPlainText(
            format_note_contents(note, subject_label=self._subject_label)
        )

    # ------------------------------------------------------------------
    # Context menu
    # ------------------------------------------------------------------

    def _on_middle_context_menu(self, pos: QtCore.QPoint) -> None:
        row = self._middle_table.rowAt(pos.y())
        if row < 0 or row >= len(self._visible_rows):
            return
        # Right-clicking outside the current selection acts on (and selects) that row.
        if row not in {idx.row() for idx in self._middle_table.selectedIndexes()}:
            self._middle_table.selectRow(row)
        menu = self._build_row_menu(row)
        menu.exec(self._middle_table.viewport().mapToGlobal(pos))

    def _build_row_menu(self, row: int) -> QtWidgets.QMenu:
        """Build the right-click menu for visible table row *row*."""
        note = self._visible_rows[row]
        menu = QtWidgets.QMenu(self)
        act_edit = menu.addAction("Edit Note")
        act_edit.triggered.connect(lambda: self._on_edit_note(note))
        act_delete = menu.addAction("Delete Note")
        act_delete.triggered.connect(lambda: self._on_delete_note(note))
        menu.addSeparator()
        act_copy = menu.addAction("Copy row")
        act_copy.triggered.connect(lambda: self._copy_middle_row(row))

        selected_rows = sorted({idx.row() for idx in self._middle_table.selectedIndexes()})
        if len(selected_rows) >= 2:
            menu.addSeparator()
            act_correlate = menu.addAction("Create Correlation\u2026")
            act_correlate.triggered.connect(lambda: self._on_create_correlation(selected_rows))
        return menu

    def _copy_middle_row(self, row_idx: int) -> None:
        if row_idx < 0 or row_idx >= len(self._owner_rows):
            return
        parts = []
        for col in range(self._middle_table.columnCount()):
            item = self._middle_table.item(row_idx, col)
            parts.append(item.text() if item else "")
        QtWidgets.QApplication.clipboard().setText("\t".join(parts))

    # ------------------------------------------------------------------
    # Edit / delete
    # ------------------------------------------------------------------

    def _writer_for(self, note: NoteRow):
        """Return a writer bound to *note*'s owner, or ``None`` (after telling the user)."""
        if self._writer is None:
            QtWidgets.QMessageBox.information(
                self, "Notes Browser", "No notes writer is available, so notes cannot be changed."
            )
            return None
        return self._writer.for_owner(note.owner_key or self._current_owner or "")

    def _on_edit_note(self, note: NoteRow) -> None:
        writer = self._writer_for(note)
        if writer is None:
            return
        dlg = EditNoteDialog(note, writer, registry=self._registry, parent=self)
        if dlg.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.refresh()

    def _on_delete_note(self, note: NoteRow) -> None:
        writer = self._writer_for(note)
        if writer is None:
            return
        preview = note.title or (note.text or "").replace("\n", " ")[:80]
        answer = QtWidgets.QMessageBox.question(
            self,
            "Delete Note",
            f"Delete this note?\n\n{preview}\n\n"
            "It is removed from the browser; the note's history stays in the notes database.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            commit_revision(
                writer,
                note,
                op=OP_DELETE,
                text=note.text,
                title=note.title,
                subject=note.subject,
            )
        except Exception as exc:
            traceback.print_exc()
            QtWidgets.QMessageBox.critical(self, "Delete Note", f"Could not delete the note:\n{exc}")
            return
        self.refresh()

    # ------------------------------------------------------------------
    # New owner / new subject
    # ------------------------------------------------------------------

    def _on_new_owner(self) -> None:
        if self._registry is None:
            return
        dlg = NewOwnerDialog(self._registry, reserved_keys=self._stored_owner_keys(), parent=self)
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted or dlg.created is None:
            return
        self._current_owner = dlg.created.key
        self._current_subject = _FILTER_ALL
        self.refresh()

    def _on_new_subject(self) -> None:
        if self._registry is None:
            return
        dlg = NewSubjectDialog(
            self._registry,
            owners=self._registry.owners(),
            default_owner=self._current_owner,
            parent=self,
        )
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted or dlg.created is None:
            return
        created = dlg.created
        if self._current_owner is not None and (
            created.owner is None or created.owner == self._current_owner
        ):
            self._current_subject = created.id
        self.refresh()

    # ------------------------------------------------------------------
    # Correlation creation
    # ------------------------------------------------------------------

    def _on_create_correlation(self, row_indices: list[int]) -> None:
        notes = [
            self._visible_rows[i] for i in row_indices
            if 0 <= i < len(self._visible_rows)
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
        if sel_row < 0 or sel_row >= len(self._visible_rows):
            return
        note = self._visible_rows[sel_row]
        entry_text = build_collection_entry(note, subject_label=self._subject_label)
        existing = self._collection_edit.toPlainText()
        if existing.strip():
            self._collection_edit.setPlainText(existing + "\n\n" + entry_text)
        else:
            header = build_collection_manifest_header(
                [note], subject_label=self._subject_label
            )
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
# Tag filter popup
# ---------------------------------------------------------------------------

class _TagFilterPopup(QtWidgets.QWidget):
    """Popup shown when the Tags column header is clicked.

    Behaves like a Google-Sheets-style column filter: all unique tag values are
    listed as checkboxes.  Unchecking a tag hides rows that carry only that tag.
    Changes are applied immediately; the popup auto-dismisses on outside click.
    """

    def __init__(
        self,
        tags: list[str],
        has_untagged: bool,
        active_filter: "set[str] | None",
        on_changed,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self._on_changed = on_changed

        # Build the canonical full list (sentinel last)
        self._all_items: list[str] = list(tags)
        if has_untagged:
            self._all_items.append(_UNTAGGED_SENTINEL)

        # Initial checked state
        if active_filter is None:
            self._checked: set[str] = set(self._all_items)
        else:
            self._checked = set(active_filter)

        self._checkboxes: dict[str, QtWidgets.QCheckBox] = {}
        self._select_all_cb: QtWidgets.QCheckBox | None = None

        self.setFixedWidth(220)
        self.setStyleSheet(
            "QCheckBox { padding: 3px 4px; }"
        )

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(4)

        # Search box
        self._search = QtWidgets.QLineEdit()
        self._search.setPlaceholderText("Search tags\u2026")
        self._search.textChanged.connect(self._on_search_changed)
        outer.addWidget(self._search)

        sep1 = QtWidgets.QFrame()
        sep1.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        outer.addWidget(sep1)

        # Select All
        self._select_all_cb = QtWidgets.QCheckBox("(Select All)")
        self._select_all_cb.setChecked(len(self._checked) == len(self._all_items))
        self._select_all_cb.toggled.connect(self._on_select_all_toggled)
        outer.addWidget(self._select_all_cb)

        sep2 = QtWidgets.QFrame()
        sep2.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        outer.addWidget(sep2)

        # Scrollable tag list
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QScrollArea.Shape.NoFrame)
        scroll.setFixedHeight(200)
        scroll.setStyleSheet("QScrollArea { border: none; }")

        self._tag_container = QtWidgets.QWidget()
        self._tag_vbox = QtWidgets.QVBoxLayout(self._tag_container)
        self._tag_vbox.setContentsMargins(0, 0, 0, 0)
        self._tag_vbox.setSpacing(1)
        scroll.setWidget(self._tag_container)
        outer.addWidget(scroll)

        self._rebuild_checkboxes(self._all_items)

    # -- internal ----------------------------------------------------------

    def _rebuild_checkboxes(self, items: list[str]) -> None:
        while self._tag_vbox.count():
            item = self._tag_vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._checkboxes = {}
        for tag in items:
            cb = QtWidgets.QCheckBox(tag)
            cb.setChecked(tag in self._checked)
            cb.toggled.connect(lambda checked, t=tag: self._on_tag_toggled(t, checked))
            self._tag_vbox.addWidget(cb)
            self._checkboxes[tag] = cb
        self._tag_vbox.addStretch()

    def _on_search_changed(self, text: str) -> None:
        text = text.strip().lower()
        if text:
            visible = [t for t in self._all_items if text in t.lower()]
        else:
            visible = list(self._all_items)
        self._rebuild_checkboxes(visible)

    def _on_select_all_toggled(self, checked: bool) -> None:
        if checked:
            self._checked = set(self._all_items)
        else:
            self._checked = set()
        for cb in self._checkboxes.values():
            cb.blockSignals(True)
            cb.setChecked(checked)
            cb.blockSignals(False)
        self._fire()

    def _on_tag_toggled(self, tag: str, checked: bool) -> None:
        if checked:
            self._checked.add(tag)
        else:
            self._checked.discard(tag)
        if self._select_all_cb is not None:
            self._select_all_cb.blockSignals(True)
            self._select_all_cb.setChecked(
                len(self._checked) == len(self._all_items)
            )
            self._select_all_cb.blockSignals(False)
        self._fire()

    def _fire(self) -> None:
        if len(self._checked) >= len(self._all_items):
            self._on_changed(None)   # all selected = no filter
        else:
            self._on_changed(set(self._checked))


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
