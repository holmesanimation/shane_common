# shane_common/ui/notes/subject_picker.py
"""Owner / subject UI pieces shared by the journal dock, the Edit Note dialog
and the Notes Browser.

SubjectPickerPopup
    Frameless popup (same behaviour as the journal's Tags picker): a "New
    subject" line edit above a list of the subjects available to one owner.
    Exactly one subject — or ``UNSET`` — is selected.

NewOwnerDialog / NewSubjectDialog
    Small prompts that validate as the user types and create the entry in the
    ``NotesRegistry``.

Requires: shane_common[qt]  (PySide6>=6.5)
"""

from __future__ import annotations

from typing import Callable

from PySide6 import QtCore, QtWidgets
from PySide6.QtCore import Qt

from shane_common.notes.notes_writer import UNSET_SUBJECT
from shane_common.notes.registry import (
    NotesRegistry,
    OwnerEntry,
    RegistryError,
    SubjectEntry,
    normalize_owner_label,
    normalize_subject_label,
    owner_key_for_label,
)

#: Combo-box data for "available to all owners" (``None`` cannot be item data).
_ALL_OWNERS = "\0all"


def subject_button_text(registry: NotesRegistry | None, subject_id: str | None) -> str:
    """Text for a Subject button: plain ``Subject`` when unset, else the label."""
    if registry is None or not subject_id or subject_id == UNSET_SUBJECT:
        return "Subject"
    return f"Subject: {registry.subject_label(subject_id)}"


# ---------------------------------------------------------------------------
# Subject picker popup
# ---------------------------------------------------------------------------

class SubjectPickerPopup(QtWidgets.QWidget):
    """Popup listing the subjects available to *owner_key*.

    Every selection or newly created subject fires ``on_changed(subject_id)``
    (``UNSET_SUBJECT`` when cleared); the popup dismisses on an outside click.
    A subject created here is scoped to *owner_key*; all-owner subjects are
    created from the Notes Browser.
    """

    def __init__(
        self,
        registry: NotesRegistry,
        owner_key: str,
        selected: str | None,
        on_changed: Callable[[str], None],
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Popup
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self._registry = registry
        self._owner_key = owner_key
        self._selected = selected or UNSET_SUBJECT
        self._on_changed = on_changed

        self.setFixedWidth(240)
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        self._edit = QtWidgets.QLineEdit()
        self._edit.setPlaceholderText("New subject…")
        self._edit.returnPressed.connect(self._add_new_subject)
        outer.addWidget(self._edit)

        self._error = QtWidgets.QLabel("")
        self._error.setWordWrap(True)
        self._error.setStyleSheet("color: #ef4444;")
        self._error.setVisible(False)
        outer.addWidget(self._error)

        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        outer.addWidget(sep)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setFixedHeight(180)
        self._container = QtWidgets.QWidget()
        self._vbox = QtWidgets.QVBoxLayout(self._container)
        self._vbox.setContentsMargins(0, 0, 0, 0)
        self._vbox.setSpacing(1)
        scroll.setWidget(self._container)
        outer.addWidget(scroll)

        self._rebuild()

    def _rebuild(self) -> None:
        while self._vbox.count():
            item = self._vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        old_group = getattr(self, "_group", None)
        if old_group is not None:
            old_group.deleteLater()
        self._group = QtWidgets.QButtonGroup(self)
        self._group.setExclusive(True)

        entries: list[tuple[str, str]] = [(UNSET_SUBJECT, UNSET_SUBJECT)]
        entries += [(s.id, s.label) for s in self._registry.subjects_for_owner(self._owner_key)]
        for sid, label in entries:
            rb = QtWidgets.QRadioButton(label)
            rb.setChecked(sid == self._selected)
            rb.toggled.connect(lambda checked, s=sid: checked and self._select(s))
            self._group.addButton(rb)
            self._vbox.addWidget(rb)
        self._vbox.addStretch()

    def _select(self, subject_id: str) -> None:
        self._selected = subject_id
        self._on_changed(subject_id)

    def _add_new_subject(self) -> None:
        try:
            entry = self._registry.add_subject(self._edit.text(), self._owner_key)
        except RegistryError as exc:
            self._error.setText(str(exc))
            self._error.setVisible(True)
            return
        self._error.setVisible(False)
        self._edit.clear()
        self._selected = entry.id
        self._on_changed(entry.id)
        self._rebuild()


def show_subject_picker(
    anchor: QtWidgets.QWidget,
    registry: NotesRegistry,
    owner_key: str,
    selected: str | None,
    on_changed: Callable[[str], None],
    *,
    above: bool = False,
) -> SubjectPickerPopup:
    """Open a ``SubjectPickerPopup`` next to *anchor* and return it (caller keeps a reference)."""
    popup = SubjectPickerPopup(registry, owner_key, selected, on_changed)
    popup.adjustSize()
    if above:
        pos = anchor.mapToGlobal(anchor.rect().topLeft())
        pos.setY(pos.y() - popup.height())
    else:
        pos = anchor.mapToGlobal(anchor.rect().bottomLeft())
    popup.move(pos)
    popup.show()
    popup.raise_()
    return popup


# ---------------------------------------------------------------------------
# Name prompts
# ---------------------------------------------------------------------------

class _NamePrompt(QtWidgets.QDialog):
    """Base: a line edit with live validation and an OK that creates the entry."""

    def __init__(self, title: str, prompt: str, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(380)
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setSpacing(8)
        self._layout.addWidget(QtWidgets.QLabel(prompt))

        self._edit = QtWidgets.QLineEdit()
        self._edit.textChanged.connect(self._revalidate)
        self._layout.addWidget(self._edit)

        self._hint = QtWidgets.QLabel("")
        self._hint.setWordWrap(True)
        self._layout.addWidget(self._hint)

        self._buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self._on_accept)
        self._buttons.rejected.connect(self.reject)
        self._layout.addWidget(self._buttons)
        self._ok = self._buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Ok)
        self._ok.setEnabled(False)

    def _show_hint(self, text: str, *, error: bool) -> None:
        self._hint.setStyleSheet("color: #ef4444;" if error else "color: #9CA3AF;")
        self._hint.setText(text)

    def _revalidate(self) -> None:
        raise NotImplementedError

    def _create(self) -> None:
        raise NotImplementedError

    def _on_accept(self) -> None:
        try:
            self._create()
        except RegistryError as exc:
            self._show_hint(str(exc), error=True)
            return
        self.accept()


class NewOwnerDialog(_NamePrompt):
    """Prompt for a new owner name.  ``created`` holds the ``OwnerEntry`` after accept."""

    def __init__(
        self,
        registry: NotesRegistry,
        reserved_keys: list[str] | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__("New Owner", "Owner name:", parent)
        self._registry = registry
        self._reserved = list(reserved_keys or [])
        self.created: OwnerEntry | None = None
        self._show_hint("Letters, numbers, spaces, hyphens and underscores.", error=False)

    def _revalidate(self) -> None:
        text = self._edit.text()
        if not text.strip():
            self._ok.setEnabled(False)
            self._show_hint("Letters, numbers, spaces, hyphens and underscores.", error=False)
            return
        try:
            label = normalize_owner_label(text)
            key = owner_key_for_label(label)
        except RegistryError as exc:
            self._ok.setEnabled(False)
            self._show_hint(str(exc), error=True)
            return
        self._ok.setEnabled(True)
        self._show_hint(f"Owner key: “{key}”.", error=False)

    def _create(self) -> None:
        self.created = self._registry.add_owner(self._edit.text(), reserved_keys=self._reserved)


class NewSubjectDialog(_NamePrompt):
    """Prompt for a new subject name and the owner it belongs to (or All)."""

    def __init__(
        self,
        registry: NotesRegistry,
        owners: list[OwnerEntry],
        default_owner: str | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__("New Subject", "Subject name:", parent)
        self._registry = registry
        self.created: SubjectEntry | None = None

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel("Available to:"))
        self._owner_combo = QtWidgets.QComboBox()
        self._owner_combo.addItem("All owners", _ALL_OWNERS)
        for o in owners:
            self._owner_combo.addItem(o.label, o.key)
        idx = self._owner_combo.findData(default_owner) if default_owner else -1
        self._owner_combo.setCurrentIndex(idx if idx >= 0 else 0)
        row.addWidget(self._owner_combo, 1)
        # Sit between the name field and the hint label.
        self._layout.insertLayout(2, row)

    def _revalidate(self) -> None:
        text = self._edit.text()
        if not text.strip():
            self._ok.setEnabled(False)
            self._show_hint("", error=False)
            return
        try:
            normalize_subject_label(text)
        except RegistryError as exc:
            self._ok.setEnabled(False)
            self._show_hint(str(exc), error=True)
            return
        self._ok.setEnabled(True)
        self._show_hint("", error=False)

    def _create(self) -> None:
        owner = self._owner_combo.currentData()
        self.created = self._registry.add_subject(
            self._edit.text(), None if owner == _ALL_OWNERS else owner
        )
