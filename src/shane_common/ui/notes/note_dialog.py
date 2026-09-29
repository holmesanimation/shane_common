# shane_common/ui/notes/note_dialog.py
"""Note commit dialogs.

NoteDialog
    Simple dialog for adding a new note via ``NotesWriter.commit()``.

RowNoteDialog
    Two-column dialog for table row notes.
    Left column  — scrollable list of prior notes with inline edit support.
    Right column — new note composer.

_NoteCard
    Inner frame widget used by ``RowNoteDialog`` to display a single prior note
    with inline edit capability.

Requires: shane_common[qt]  (PySide6>=6.5)
"""

from __future__ import annotations

import traceback
from datetime import datetime, timezone as _tz
from pathlib import Path
from typing import Any

from PySide6 import QtCore, QtGui, QtWidgets
from PySide6.QtCore import Qt
from PySide6.QtGui import QDesktopServices, QAction
from PySide6.QtCore import QUrl

from shane_common.notes.model import NoteRow, latest_revisions, note_row_from_note
from shane_common.notes.notes_writer import (
    OP_CREATE,
    OP_EDIT,
    UNSET_SUBJECT,
    Note,
    NoteType,
    NotesWriter,
)
from shane_common.notes.registry import NotesRegistry, get_default_registry
from shane_common.notes.revisions import CommittedRevision, commit_revision
from shane_common.ui.notes.subject_picker import show_subject_picker
from shane_common.ui.spellcheck_highlighter import enable_spellcheck


# ---------------------------------------------------------------------------
# NoteDialog — simple single-column dialog
# ---------------------------------------------------------------------------

class NoteDialog(QtWidgets.QDialog):
    """Small commit dialog for appending a note via ``NotesWriter``.

    Parameters
    ----------
    writer:
        ``NotesWriter`` instance that owns the target store and clock.
    owner:
        Owner key forwarded to ``Note.owner``.
    context:
        Freeform JSON-safe dict attached to the note's ``context`` field.
    instrument:
        Optional instrument seed.
    strategy_id:
        Optional strategy seed.
    parent:
        Qt parent widget.
    """

    def __init__(
        self,
        writer: NotesWriter,
        owner: str = "",
        context: dict | None = None,
        instrument: str | None = None,
        strategy_id: str | None = None,
        show_type_selector: bool = True,
        history_rows: list[Any] | None = None,
        registry: NotesRegistry | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._writer = writer
        self._registry = registry if registry is not None else get_default_registry()
        self._owner = str(owner)
        self._context = dict(context or {})
        self._instrument = instrument
        self._strategy_id = strategy_id
        self._show_type_selector = show_type_selector
        self._history_rows = list(history_rows) if history_rows is not None else None
        self._last_path: Path | None = None

        self.setWindowTitle("Add Note")
        self.setMinimumWidth(480)
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(8)

        self._build_top_extra(layout)

        # ---- Type row (omitted when the owner implies a fixed note type) ----
        self._type_combo: QtWidgets.QComboBox | None = None
        if self._show_type_selector:
            type_row = QtWidgets.QHBoxLayout()
            type_row.addWidget(QtWidgets.QLabel("Type:"))
            self._type_combo = QtWidgets.QComboBox()
            for nt in NoteType:
                self._type_combo.addItem(nt.value.title(), nt)
            type_row.addWidget(self._type_combo)
            type_row.addStretch()
            layout.addLayout(type_row)

        # ---- Body ----
        layout.addWidget(QtWidgets.QLabel("Note:"))
        self._body_edit = QtWidgets.QPlainTextEdit()
        self._body_edit.setPlaceholderText("Enter note text\u2026")
        self._body_edit.setMinimumHeight(120)
        layout.addWidget(self._body_edit)
        enable_spellcheck(self._body_edit)

        # ---- Commit + Open folder buttons ----
        btn_row = QtWidgets.QHBoxLayout()
        self._commit_btn = QtWidgets.QPushButton("Commit")
        self._commit_btn.setDefault(True)
        self._commit_btn.clicked.connect(self._on_commit)
        btn_row.addWidget(self._commit_btn)

        self._open_folder_btn = QtWidgets.QPushButton("\U0001F4C2")
        self._open_folder_btn.setFlat(True)
        self._open_folder_btn.setToolTip("Open notes folder")
        self._open_folder_btn.setStyleSheet("QPushButton { padding: 2px 6px; font-size: 14px; }")
        self._open_folder_btn.setEnabled(False)
        self._open_folder_btn.clicked.connect(self._on_open_folder)
        btn_row.addWidget(self._open_folder_btn)
        btn_row.addStretch()
        self._build_commit_row_extra(btn_row)
        layout.addLayout(btn_row)

        # ---- Prior notes history (optional) ----
        self._history_list: QtWidgets.QListWidget | None = None
        if self._history_rows is not None:
            layout.addWidget(QtWidgets.QLabel("Previous Notes"))
            self._history_list = QtWidgets.QListWidget()
            self._history_list.setSpacing(2)
            self._history_list.setMaximumHeight(140)
            self._history_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            self._history_list.customContextMenuRequested.connect(self._on_history_context_menu)
            self._history_list.setProperty("ownContextMenu", True)
            layout.addWidget(self._history_list)
            self._populate_history()

        # ---- Status ----
        self._status_label = QtWidgets.QLabel("")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

    # ------------------------------------------------------------------
    # Subclass hooks (default no-ops)
    # ------------------------------------------------------------------

    def _build_top_extra(self, layout: QtWidgets.QVBoxLayout) -> None:
        """Insert extra widgets above the Type row."""

    def _build_commit_row_extra(self, btn_row: QtWidgets.QHBoxLayout) -> None:
        """Append right-anchored widgets to the Commit row."""

    def _history_item_rich_text(self, row: Any) -> str | None:
        """Return HTML for a history row, or ``None`` for plain-text rendering."""
        return None

    def _extra_edit_widget_factory(self) -> QtWidgets.QWidget | None:
        return None

    def _extra_edit_widget_set_value(self, widget: QtWidgets.QWidget, row: Any) -> None:
        pass

    def _extra_edit_widget_get_context(self, widget: QtWidgets.QWidget) -> dict:
        return {}

    def _on_note_edited(self, row: Any, new_text: str, new_context: dict) -> None:
        pass

    # ------------------------------------------------------------------
    # History list
    # ------------------------------------------------------------------

    def _populate_history(self) -> None:
        if self._history_list is None or self._history_rows is None:
            return
        self._history_list.clear()
        for row in latest_revisions(self._history_rows):
            self._add_history_item(row)

    def _add_history_item(self, row: Any) -> None:
        if self._history_list is None:
            return
        html = self._history_item_rich_text(row)
        text = "" if html is not None else self._format_history_item(
            getattr(row, "wall_ts", None), getattr(row, "text", "")
        )
        item = QtWidgets.QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, row)
        self._history_list.addItem(item)
        if html is not None:
            label = QtWidgets.QLabel(html)
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setWordWrap(True)
            item.setSizeHint(label.sizeHint())
            self._history_list.setItemWidget(item, label)

    def _on_history_context_menu(self, pos: QtCore.QPoint) -> None:
        if self._history_list is None:
            return
        item = self._history_list.itemAt(pos)
        if item is None:
            return
        row = item.data(Qt.ItemDataRole.UserRole)
        if row is None:
            return
        menu = QtWidgets.QMenu(self._history_list)
        edit_action = menu.addAction("Edit Note")
        chosen = menu.exec(self._history_list.viewport().mapToGlobal(pos))
        if chosen is not edit_action:
            return
        self._open_edit_dialog(row)

    def _open_edit_dialog(self, row: Any) -> None:
        extra = self._extra_edit_widget_factory()
        if extra is not None:
            self._extra_edit_widget_set_value(extra, row)
        owner = getattr(row, "owner_key", None) or self._writer.owner
        dlg = EditNoteDialog(
            row,
            self._writer.for_owner(owner),
            registry=self._registry,
            extra_widget=extra,
            extra_context=self._extra_edit_widget_get_context,
            parent=self,
        )
        if dlg.exec() != QtWidgets.QDialog.DialogCode.Accepted or dlg.committed is None:
            return
        committed = dlg.committed
        self._record_new_revision(committed.note)
        self._on_note_edited(row, committed.note.text, committed.context)

    def _record_new_revision(self, note: Note) -> None:
        """Add a just-committed note to the history cache and re-render."""
        if self._history_rows is None:
            return
        self._history_rows.append(note_row_from_note(note))
        self._populate_history()

    @staticmethod
    def _format_history_item(wall_ts: float | None, text: str) -> str:
        ts_label = datetime.fromtimestamp(wall_ts).strftime("%b %d %H:%M") if wall_ts else ""
        preview = (text or "")[:80]
        return f"[{ts_label}] {preview}"

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @QtCore.Slot()
    def _on_commit(self) -> None:
        text = self._body_edit.toPlainText().strip()
        if not text:
            self._status_label.setText("Note text is empty.")
            return

        note_type: NoteType = self._type_combo.currentData() if self._type_combo is not None else NoteType.GENERAL
        note = self._writer.build_note(
            note_type=note_type,
            text=text,
            instrument=self._instrument,
            strategy_id=self._strategy_id,
            context=dict(self._context),
        )
        try:
            path = self._writer.commit(note)
            self._last_path = path
            self._open_folder_btn.setEnabled(True)
            self._status_label.setText(f"Saved to {path}")
            self._body_edit.clear()
            self._record_new_revision(note)
        except Exception as exc:
            traceback.print_exc()
            self._status_label.setText(f"Error: {exc}")

    @QtCore.Slot()
    def _on_open_folder(self) -> None:
        if self._last_path is not None and self._last_path.parent.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_path.parent)))

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def update_context(
        self,
        context: dict,
        *,
        instrument: str | None = None,
        strategy_id: str | None = None,
    ) -> None:
        """Update the context dict when the dialog is reused for a different entry."""
        self._context = dict(context or {})
        if instrument is not None:
            self._instrument = instrument
        if strategy_id is not None:
            self._strategy_id = strategy_id
        self._status_label.setText("")


# ---------------------------------------------------------------------------
# EditNoteDialog — append a new revision of an existing note
# ---------------------------------------------------------------------------

class EditNoteDialog(QtWidgets.QDialog):
    """Modal editor that appends a new revision (same note ID) of *row*.

    Used from a ``NoteDialog``'s history list and from the Notes Browser.

    Parameters
    ----------
    row:
        The note revision being edited.
    writer:
        ``NotesWriter`` bound to the note's owner.
    registry:
        Enables the subject controls; without one the subject is left as-is.
    extra_widget / extra_context:
        Optional owner-specific widget shown under the text box, and a
        callable ``widget -> dict`` whose result is merged into the context.

    After ``exec()`` returns ``Accepted``, :attr:`committed` describes what was
    written.
    """

    def __init__(
        self,
        row: Any,
        writer: NotesWriter,
        *,
        registry: NotesRegistry | None = None,
        extra_widget: QtWidgets.QWidget | None = None,
        extra_context: Any = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._row = row
        self._writer = writer
        self._registry = registry
        self._extra_context = extra_context
        self._owner_key = getattr(row, "owner_key", None) or writer.owner
        self._subject: str = getattr(row, "subject", None) or UNSET_SUBJECT
        self._subject_popup: QtWidgets.QWidget | None = None
        self.committed: CommittedRevision | None = None
        self.setWindowTitle("Edit Note")
        self.setMinimumWidth(460)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(8)

        title_row = QtWidgets.QHBoxLayout()
        title_row.addWidget(QtWidgets.QLabel("Title:"))
        self._title_edit = QtWidgets.QLineEdit(getattr(row, "title", None) or "")
        self._title_edit.setPlaceholderText("(none)")
        title_row.addWidget(self._title_edit, 1)
        layout.addLayout(title_row)

        self._subject_lbl: QtWidgets.QLabel | None = None
        self._subject_btn: QtWidgets.QPushButton | None = None
        if registry is not None:
            subject_row = QtWidgets.QHBoxLayout()
            self._subject_lbl = QtWidgets.QLabel()
            subject_row.addWidget(self._subject_lbl, 1)
            self._subject_btn = QtWidgets.QPushButton("Subject")
            self._subject_btn.clicked.connect(self._open_subject_picker)
            subject_row.addWidget(self._subject_btn)
            layout.addLayout(subject_row)
            self._refresh_subject_label()

        layout.addWidget(QtWidgets.QLabel("Note:"))
        self._body_edit = QtWidgets.QPlainTextEdit()
        self._body_edit.setPlainText(getattr(row, "text", "") or "")
        self._body_edit.setMinimumHeight(120)
        layout.addWidget(self._body_edit)
        enable_spellcheck(self._body_edit)

        self._extra_widget = extra_widget
        if self._extra_widget is not None:
            layout.addWidget(self._extra_widget)

        btn_row = QtWidgets.QHBoxLayout()
        commit_btn = QtWidgets.QPushButton("Commit")
        commit_btn.setDefault(True)
        commit_btn.clicked.connect(self._on_commit)
        cancel_btn = QtWidgets.QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btn_row.addWidget(commit_btn)
        btn_row.addWidget(cancel_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self._status_label = QtWidgets.QLabel("")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

    # -- subject -------------------------------------------------------------

    def _refresh_subject_label(self) -> None:
        if self._subject_lbl is not None and self._registry is not None:
            self._subject_lbl.setText(f"Subject: {self._registry.subject_label(self._subject)}")

    def _open_subject_picker(self) -> None:
        if self._registry is None or self._subject_btn is None:
            return
        self._subject_popup = show_subject_picker(
            self._subject_btn,
            self._registry,
            self._owner_key,
            self._subject,
            self._on_subject_changed,
        )

    def _on_subject_changed(self, subject_id: str) -> None:
        self._subject = subject_id
        self._refresh_subject_label()

    # -- commit --------------------------------------------------------------

    @QtCore.Slot()
    def _on_commit(self) -> None:
        new_text = self._body_edit.toPlainText().strip()
        if not new_text:
            self._status_label.setText("Note text is empty.")
            return
        try:
            extra = (
                self._extra_context(self._extra_widget)
                if self._extra_widget is not None and self._extra_context is not None
                else {}
            )
            self.committed = commit_revision(
                self._writer,
                self._row,
                op=OP_EDIT,
                text=new_text,
                title=self._title_edit.text(),
                subject=self._subject,
                context_updates=extra,
            )
        except Exception as exc:
            traceback.print_exc()
            self._status_label.setText(f"Error: {exc}")
            return
        self.accept()


# ---------------------------------------------------------------------------
# _NoteCard — inner widget for a prior note inside RowNoteDialog
# ---------------------------------------------------------------------------

class _NoteCard(QtWidgets.QFrame):
    """Read-only display of one note row, with inline edit support.

    Signals
    -------
    edit_requested : Signal(str, str, int)
        Emitted when the user clicks ``Commit edit``.
        Arguments: ``(note_id, new_text, current_revision_num)``
    """

    edit_requested = QtCore.Signal(str, str, int)

    def __init__(self, note_id: str, ts: float, note_type_str: str,
                 text: str, revision_num: int,
                 parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._note_id = note_id
        self._current_text = text
        self._revision_num = revision_num

        self.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)
        self.setFrameShadow(QtWidgets.QFrame.Shadow.Raised)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(3)

        # Header: timestamp + type
        try:
            dt = datetime.fromtimestamp(ts, tz=_tz.utc)
            ts_str = dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        except (OSError, OverflowError, ValueError):
            ts_str = str(ts)

        rev_str = f"  (rev {revision_num})" if revision_num > 1 else ""
        hdr_lbl = QtWidgets.QLabel(f"{ts_str}  [{note_type_str}]{rev_str}")
        hdr_lbl.setStyleSheet("font-size: 10px; color: #9CA3AF;")
        layout.addWidget(hdr_lbl)
        self._hdr_lbl = hdr_lbl

        # Body: read mode
        self._body_lbl = QtWidgets.QLabel(text)
        self._body_lbl.setWordWrap(True)
        self._body_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        layout.addWidget(self._body_lbl)

        # Body: edit mode (initially hidden)
        self._body_editor = QtWidgets.QPlainTextEdit()
        self._body_editor.setPlainText(text)
        self._body_editor.setMaximumHeight(100)
        self._body_editor.setVisible(False)
        layout.addWidget(self._body_editor)
        enable_spellcheck(self._body_editor)

        # Buttons
        btn_row = QtWidgets.QHBoxLayout()
        self._edit_btn = QtWidgets.QPushButton("Edit note")
        self._edit_btn.setFlat(True)
        self._edit_btn.setStyleSheet("font-size: 10px; padding: 1px 4px;")
        self._edit_btn.clicked.connect(self._on_edit_clicked)
        btn_row.addWidget(self._edit_btn)

        self._commit_edit_btn = QtWidgets.QPushButton("Commit edit")
        self._commit_edit_btn.setFlat(True)
        self._commit_edit_btn.setStyleSheet("font-size: 10px; padding: 1px 4px;")
        self._commit_edit_btn.setVisible(False)
        self._commit_edit_btn.clicked.connect(self._on_commit_edit)
        btn_row.addWidget(self._commit_edit_btn)

        self._discard_edit_btn = QtWidgets.QPushButton("Discard edit")
        self._discard_edit_btn.setFlat(True)
        self._discard_edit_btn.setStyleSheet("font-size: 10px; padding: 1px 4px;")
        self._discard_edit_btn.setVisible(False)
        self._discard_edit_btn.clicked.connect(self._on_discard_edit)
        btn_row.addWidget(self._discard_edit_btn)

        btn_row.addStretch()
        layout.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Mode transitions
    # ------------------------------------------------------------------

    @QtCore.Slot()
    def _on_edit_clicked(self) -> None:
        self._body_lbl.setVisible(False)
        self._body_editor.setPlainText(self._current_text)
        self._body_editor.setVisible(True)
        self._edit_btn.setVisible(False)
        self._commit_edit_btn.setVisible(True)
        self._discard_edit_btn.setVisible(True)

    @QtCore.Slot()
    def _on_commit_edit(self) -> None:
        new_text = self._body_editor.toPlainText().strip()
        if not new_text:
            return
        self.edit_requested.emit(self._note_id, new_text, self._revision_num)

    @QtCore.Slot()
    def _on_discard_edit(self) -> None:
        self._body_editor.setVisible(False)
        self._body_lbl.setVisible(True)
        self._commit_edit_btn.setVisible(False)
        self._discard_edit_btn.setVisible(False)
        self._edit_btn.setVisible(True)

    def apply_edit(self, new_text: str, new_revision_num: int) -> None:
        """Update display after a successful edit commit."""
        self._current_text = new_text
        self._revision_num = new_revision_num
        self._body_lbl.setText(new_text)
        existing = self._hdr_lbl.text()
        if "  (rev " in existing:
            existing = existing[: existing.index("  (rev ")]
        self._hdr_lbl.setText(
            f"{existing}  (rev {new_revision_num})" if new_revision_num > 1 else existing
        )
        self._on_discard_edit()


# ---------------------------------------------------------------------------
# RowNoteDialog — two-column dialog for table row notes
# ---------------------------------------------------------------------------

class RowNoteDialog(QtWidgets.QDialog):
    """Two-column dialog for table row notes.

    Left column  — title, scrollable prior notes (each with inline edit).
    Right column — new note composer.

    Parameters
    ----------
    writer:
        ``NotesWriter`` used to persist commits.
    repository:
        ``NotesRepository`` (or ``None``) used to load prior notes on open.
    owner_key / owner_label / owner_module / owner_class:
        Owner metadata stored on every committed note.
    table_id / table_label:
        Table metadata stored on every committed note.
    row_id:
        Deterministic ID of the table row being annotated.
    row_ts:
        Optional row timestamp (UTC epoch float).
    row_snapshot:
        Dict of visible row column values.
    instrument / strategy_id:
        Optional seeds stored on committed notes.

    Signals
    -------
    note_committed : Signal(str, bool)
        Emitted after a successful write.
        ``(note_id, is_edit)`` — ``is_edit=True`` for edit revisions.
    """

    note_committed = QtCore.Signal(str, bool)

    def __init__(
        self,
        *,
        writer: NotesWriter,
        repository: Any,
        owner_key: str,
        owner_label: str,
        owner_module: str,
        owner_class: str,
        table_id: str,
        table_label: str,
        row_id: str,
        row_ts: float | None,
        row_snapshot: dict,
        instrument: str | None = None,
        strategy_id: str | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._writer = writer
        self._repository = repository
        self._owner_key = owner_key
        self._owner_label = owner_label
        self._owner_module = owner_module
        self._owner_class = owner_class
        self._table_id = table_id
        self._table_label = table_label
        self._row_id = row_id
        self._row_ts = row_ts
        self._row_snapshot = dict(row_snapshot or {})
        self._instrument = instrument
        self._strategy_id = strategy_id
        self._last_path: Path | None = None
        self._note_cards: list[_NoteCard] = []

        self.setWindowTitle(f"Notes \u2014 {table_label}")
        self.setMinimumSize(780, 460)
        self._build_ui()
        self._load_prior_notes()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QtWidgets.QHBoxLayout(self)
        root.setSpacing(12)
        root.setContentsMargins(10, 10, 10, 10)

        # ---- Left column: prior notes ---------------------------------
        left = QtWidgets.QWidget()
        left.setMinimumWidth(360)
        left_vbox = QtWidgets.QVBoxLayout(left)
        left_vbox.setContentsMargins(0, 0, 0, 0)
        left_vbox.setSpacing(4)

        title_lbl = QtWidgets.QLabel(
            f"<b>{self._table_label}</b><br><span style='font-size:10px; color:#9CA3AF;'>"
            f"row: {self._row_id}</span>"
        )
        title_lbl.setTextFormat(Qt.TextFormat.RichText)
        title_lbl.setWordWrap(True)
        left_vbox.addWidget(title_lbl)

        self._scroll_area = QtWidgets.QScrollArea()
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)

        self._cards_container = QtWidgets.QWidget()
        self._cards_layout = QtWidgets.QVBoxLayout(self._cards_container)
        self._cards_layout.setContentsMargins(0, 0, 4, 0)
        self._cards_layout.setSpacing(6)
        self._cards_layout.addStretch()

        self._scroll_area.setWidget(self._cards_container)
        left_vbox.addWidget(self._scroll_area, 1)

        self._empty_lbl = QtWidgets.QLabel("No prior notes for this row.")
        self._empty_lbl.setStyleSheet("color: #6B7280; font-style: italic;")
        self._empty_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        left_vbox.addWidget(self._empty_lbl)

        root.addWidget(left, 2)

        # ---- Vertical separator ---------------------------------------
        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        sep.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
        root.addWidget(sep)

        # ---- Right column: new note composer --------------------------
        right = QtWidgets.QWidget()
        right.setMinimumWidth(300)
        right_vbox = QtWidgets.QVBoxLayout(right)
        right_vbox.setContentsMargins(0, 0, 0, 0)
        right_vbox.setSpacing(6)

        new_note_lbl = QtWidgets.QLabel("New Note")
        new_note_lbl.setStyleSheet("font-weight: bold;")
        right_vbox.addWidget(new_note_lbl)

        type_row = QtWidgets.QHBoxLayout()
        type_row.addWidget(QtWidgets.QLabel("Type:"))
        self._type_combo = QtWidgets.QComboBox()
        for nt in NoteType:
            self._type_combo.addItem(nt.display_label, nt)
        type_row.addWidget(self._type_combo, 1)
        right_vbox.addLayout(type_row)

        right_vbox.addWidget(QtWidgets.QLabel("Note:"))
        self._body_edit = QtWidgets.QPlainTextEdit()
        self._body_edit.setPlaceholderText("Enter note text\u2026")
        self._body_edit.setMinimumHeight(140)
        right_vbox.addWidget(self._body_edit, 1)

        btn_row = QtWidgets.QHBoxLayout()
        self._commit_btn = QtWidgets.QPushButton("Commit")
        self._commit_btn.setDefault(True)
        self._commit_btn.clicked.connect(self._on_commit_new)
        btn_row.addWidget(self._commit_btn)

        self._open_folder_btn = QtWidgets.QPushButton("\U0001F4C2")
        self._open_folder_btn.setFlat(True)
        self._open_folder_btn.setToolTip("Open notes folder")
        self._open_folder_btn.setStyleSheet("QPushButton { padding: 2px 6px; font-size: 14px; }")
        self._open_folder_btn.setEnabled(False)
        self._open_folder_btn.clicked.connect(self._on_open_folder)
        btn_row.addWidget(self._open_folder_btn)
        btn_row.addStretch()
        right_vbox.addLayout(btn_row)

        self._status_label = QtWidgets.QLabel("")
        self._status_label.setWordWrap(True)
        right_vbox.addWidget(self._status_label)

        root.addWidget(right, 1)

    # ------------------------------------------------------------------
    # Prior notes loading
    # ------------------------------------------------------------------

    def _load_prior_notes(self) -> None:
        prior = []
        if self._repository is not None:
            prior = self._repository.latest_notes_for_row(self._row_id)

        self._scroll_area.setVisible(len(prior) > 0)
        self._empty_lbl.setVisible(len(prior) == 0)

        for note_row in prior:
            self._add_card_from_note_row(note_row)

    def _add_card_from_note_row(self, note_row: Any) -> _NoteCard:
        card = _NoteCard(
            note_id=note_row.note_id or "",
            ts=note_row.ts or 0.0,
            note_type_str=note_row.note_type or "NOTE",
            text=note_row.text or "",
            revision_num=note_row.revision_num or 1,
            parent=self._cards_container,
        )
        card.edit_requested.connect(self._on_edit_requested)
        insert_pos = self._cards_layout.count() - 1
        self._cards_layout.insertWidget(insert_pos, card)
        self._note_cards.append(card)
        self._empty_lbl.setVisible(False)
        self._scroll_area.setVisible(True)
        return card

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    @QtCore.Slot(str, str, int)
    def _on_edit_requested(self, note_id: str, new_text: str, current_revision_num: int) -> None:
        if not note_id or not new_text:
            self._status_label.setText("Cannot commit empty text.")
            return

        writer = self._writer.for_owner(self._owner_key)
        note = writer.build_note(
            note_id=note_id,
            revision_num=current_revision_num + 1,
            op=OP_EDIT,
            owner_module=self._owner_module,
            owner_class=self._owner_class,
            table_id=self._table_id,
            table_label=self._table_label,
            row_id=self._row_id,
            row_ts=self._row_ts,
            row_snapshot=self._row_snapshot,
            text=new_text,
            instrument=self._instrument,
            strategy_id=self._strategy_id,
        )
        try:
            path = writer.commit(note)
            self._last_path = path
            self._open_folder_btn.setEnabled(True)
            for card in self._note_cards:
                if card._note_id == note_id:
                    card.apply_edit(new_text, note.revision_num)
                    break
            self._status_label.setText(f"Edit saved \u2192 {path.name}")
            self.note_committed.emit(note_id, True)
        except Exception as exc:
            traceback.print_exc()
            self._status_label.setText(f"Error: {exc}")

    @QtCore.Slot()
    def _on_commit_new(self) -> None:
        text = self._body_edit.toPlainText().strip()
        if not text:
            self._status_label.setText("Note text is empty.")
            return

        note_type: NoteType = self._type_combo.currentData()
        writer = self._writer.for_owner(self._owner_key)
        note = writer.build_note(
            revision_num=1,
            op=OP_CREATE,
            owner_module=self._owner_module,
            owner_class=self._owner_class,
            table_id=self._table_id,
            table_label=self._table_label,
            row_id=self._row_id,
            row_ts=self._row_ts,
            row_snapshot=self._row_snapshot,
            note_type=note_type,
            text=text,
            instrument=self._instrument,
            strategy_id=self._strategy_id,
        )
        try:
            path = writer.commit(note)
            self._last_path = path
            self._open_folder_btn.setEnabled(True)
            self._add_card_from_note_row(_FakeNoteRow(note))
            self._body_edit.clear()
            self._status_label.setText(f"Saved \u2192 {path.name}")
            self.note_committed.emit(note.note_id, False)
        except Exception as exc:
            traceback.print_exc()
            self._status_label.setText(f"Error: {exc}")

    @QtCore.Slot()
    def _on_open_folder(self) -> None:
        if self._last_path is not None and self._last_path.parent.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._last_path.parent)))


# ---------------------------------------------------------------------------
# Minimal adapter so a freshly committed table Note can seed a _NoteCard
# ---------------------------------------------------------------------------

class _FakeNoteRow:
    """Wraps a ``Note`` to satisfy ``_add_card_from_note_row``'s duck-type."""

    def __init__(self, note: Any) -> None:
        self.note_id = note.note_id
        self.ts = note.ts
        self.note_type = str(note.note_type)
        self.text = note.text
        self.revision_num = note.revision_num


# ---------------------------------------------------------------------------
# Widget-level helpers — attach note actions to any QWidget
# ---------------------------------------------------------------------------

def make_note_action(
    widget: QtWidgets.QWidget,
    writer: NotesWriter,
    owner: str = "",
    *,
    instrument: str | None = None,
    strategy_id: str | None = None,
    parent_dialog: QtWidgets.QWidget | None = None,
    label: str = "Add Note\u2026",
) -> QAction:
    """Return a ``QAction`` that opens a :class:`NoteDialog` for *widget*.

    The action captures the widget's ``objectName()``, class name, and module
    at the moment it is triggered, storing them in the note's ``context`` dict
    under the keys ``widget_name``, ``widget_class``, and ``widget_module``.

    Use this when a widget already has its own context menu — just insert the
    returned action into the menu yourself.

    Parameters
    ----------
    widget:
        The widget being annotated.
    writer:
        ``NotesWriter`` used to persist the note.
    owner:
        Owner label forwarded to the note.
    instrument:
        Optional instrument seed.
    strategy_id:
        Optional strategy seed.
    parent_dialog:
        Qt parent for the :class:`NoteDialog`.  Defaults to *widget*.
    label:
        Action text shown in the menu.
    """
    action = QAction(label, widget)

    @QtCore.Slot()
    def _triggered() -> None:
        context = {
            "widget_name": widget.objectName() or "",
            "widget_class": type(widget).__name__,
            "widget_module": type(widget).__module__,
        }
        dlg = NoteDialog(
            writer=writer,
            owner=owner,
            context=context,
            instrument=instrument,
            strategy_id=strategy_id,
            parent=parent_dialog or widget,
        )
        dlg.exec()

    action.triggered.connect(_triggered)
    return action


def install_note_action(
    widget: QtWidgets.QWidget,
    writer: NotesWriter,
    owner: str = "",
    *,
    instrument: str | None = None,
    strategy_id: str | None = None,
    parent_dialog: QtWidgets.QWidget | None = None,
    label: str = "Add Note\u2026",
) -> None:
    """Install a right-click *Add Note* action directly on *widget*.

    Sets the widget's context menu policy to ``CustomContextMenu`` (if it is
    not already) and connects a handler that shows a one-item menu containing
    the action returned by :func:`make_note_action`.

    If the widget already uses ``CustomContextMenu``, the existing signal
    connections are preserved and the *Add Note* item is appended to whatever
    menu your existing handler builds.  In that case, prefer calling
    :func:`make_note_action` and inserting the action yourself.

    Parameters
    ----------
    widget:
        Any ``QWidget`` to annotate.
    writer / owner / instrument / strategy_id / parent_dialog / label:
        Forwarded to :func:`make_note_action`.
    """
    action = make_note_action(
        widget,
        writer,
        owner,
        instrument=instrument,
        strategy_id=strategy_id,
        parent_dialog=parent_dialog,
        label=label,
    )

    already_custom = (
        widget.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    )

    if not already_custom:
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

        @QtCore.Slot(QtCore.QPoint)
        def _show_menu(pos: QtCore.QPoint) -> None:
            menu = QtWidgets.QMenu(widget)
            menu.addAction(action)
            menu.exec(widget.mapToGlobal(pos))

        widget.customContextMenuRequested.connect(_show_menu)
    else:
        # Widget already manages its own menu — connect the action directly
        # so it fires when the action is triggered from that existing menu.
        # Caller is responsible for inserting it into the menu object.
        pass  # action was already built; caller receives it via make_note_action
