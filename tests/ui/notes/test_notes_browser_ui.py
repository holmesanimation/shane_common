"""Notes Browser: owner/subject lists, filters, context menu, edit/delete, prompts."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6 import QtCore, QtWidgets  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402

from shane_common.notes.notes_repository import NotesRepository, latest_revisions  # noqa: E402
from shane_common.notes.notes_writer import UNSET_SUBJECT, NotesWriter, NoteType  # noqa: E402
from shane_common.notes.registry import NotesRegistry  # noqa: E402
from shane_common.notes.store import NotesStore  # noqa: E402
from shane_common.ui.notes.note_dialog import EditNoteDialog  # noqa: E402
from shane_common.ui.notes.notes_browser_window import NotesBrowserWindow  # noqa: E402
from shane_common.ui.notes.subject_picker import (  # noqa: E402
    NewOwnerDialog,
    NewSubjectDialog,
    SubjectPickerPopup,
)

BUILTINS = (("journal", "journal"), ("Prayer", "Prayer"))


@pytest.fixture(scope="module", autouse=True)
def _app():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app


class Env:
    def __init__(self, tmp_path: Path) -> None:
        self.store = NotesStore(tmp_path / "notes.db")
        self.registry = NotesRegistry(self.store, builtin_owners=BUILTINS)
        self.writer = NotesWriter(self.store, "journal")
        self.repo = NotesRepository(self.store)

    def add(self, owner: str, text: str, ts: float, **extra) -> None:
        """Store a note with a fixed timestamp (``extra``: title / subject)."""
        writer = self.writer.for_owner(owner)
        note = writer.build_note(text=text, **extra)
        writer.commit(dataclasses.replace(note, ts=ts, wall_ts=ts))

    def browser(self) -> NotesBrowserWindow:
        win = NotesBrowserWindow(self.repo, registry=self.registry)
        win.set_writer(self.writer)
        return win


@pytest.fixture
def env(tmp_path: Path):
    e = Env(tmp_path)
    yield e
    e.store.close()


def _items(lw: QtWidgets.QListWidget) -> list[str]:
    return [lw.item(i).text() for i in range(lw.count())]


def _select(lw: QtWidgets.QListWidget, text: str) -> None:
    lw.setCurrentItem(lw.findItems(text, Qt.MatchFlag.MatchExactly)[0])


def _titles(win: NotesBrowserWindow) -> list[str]:
    t = win._middle_table
    return [t.item(r, 3).text() for r in range(t.rowCount())]


# -- lists --------------------------------------------------------------------

def test_owner_list_has_builtins_and_created_owners_even_without_notes(env: Env) -> None:
    env.registry.add_owner("Sleep Log")
    win = env.browser()
    assert set(_items(win._owner_list)) >= {"journal", "Prayer", "Sleep Log"}


def test_subject_list_order_all_unset_then_owner_subjects(env: Env) -> None:
    env.registry.add_subject("Zeta", "journal")
    env.registry.add_subject("Alpha", "journal")
    env.registry.add_subject("Shared", None)
    env.registry.add_subject("PrayerOnly", "Prayer")
    env.add("journal", "n", 1.0)
    win = env.browser()
    _select(win._owner_list, "journal")
    assert _items(win._subject_list) == ["ALL", "UNSET", "Alpha", "Shared", "Zeta"]
    _select(win._owner_list, "Prayer")
    assert _items(win._subject_list) == ["ALL", "UNSET", "PrayerOnly", "Shared"]


def test_subject_filtering_all_unset_and_specific(env: Env) -> None:
    sub = env.registry.add_subject("Anxiety", "journal")
    env.add("journal", "old one", 1.0)  # subject left at its default (UNSET)
    env.add("journal", "old two", 2.0, subject=None)
    env.add("journal", "tagged", 3.0, subject=sub.id)
    win = env.browser()
    _select(win._owner_list, "journal")

    assert _titles(win) == ["tagged", "old two", "old one"]  # ALL is the default; newest first
    _select(win._subject_list, "UNSET")
    assert _titles(win) == ["old two", "old one"]
    _select(win._subject_list, "Anxiety")
    assert _titles(win) == ["tagged"]
    _select(win._subject_list, "ALL")
    assert len(_titles(win)) == 3


def test_switching_owner_resets_subject_to_all(env: Env) -> None:
    env.registry.add_subject("Anxiety", "journal")
    env.add("journal", "a", 1.0)
    env.add("Prayer", "p", 2.0)
    win = env.browser()
    _select(win._owner_list, "journal")
    _select(win._subject_list, "UNSET")
    _select(win._owner_list, "Prayer")
    assert win._subject_list.currentItem().text() == "ALL"
    assert _titles(win) == ["p"]


def test_title_column_shows_title_else_text_preview(env: Env) -> None:
    env.add("journal", "no title here", 1.0)
    env.add("journal", "body", 2.0, title="Real Title")
    win = env.browser()
    _select(win._owner_list, "journal")
    assert _titles(win) == ["Real Title", "no title here"]


# -- context menu -------------------------------------------------------------

def test_table_opts_out_of_app_wide_add_note_menu(env: Env) -> None:
    win = env.browser()
    assert win._middle_table.property("ownContextMenu") is True


def test_context_menu_offers_edit_and_delete(env: Env) -> None:
    env.add("journal", "n", 1.0)
    win = env.browser()
    _select(win._owner_list, "journal")
    labels = [a.text() for a in win._build_row_menu(0).actions() if a.text()]
    assert labels[:2] == ["Edit Note", "Delete Note"]
    assert "Add Note" not in " ".join(labels)
    assert "Browse Notes" not in " ".join(labels)


def test_context_menu_edit_action_opens_edit_dialog(env: Env, monkeypatch) -> None:
    env.add("journal", "n", 1.0)
    win = env.browser()
    _select(win._owner_list, "journal")
    opened: list = []
    monkeypatch.setattr(win, "_on_edit_note", opened.append)
    next(a for a in win._build_row_menu(0).actions() if a.text() == "Edit Note").trigger()
    assert opened == [win._visible_rows[0]]


# -- edit ---------------------------------------------------------------------

def test_edit_note_sets_subject_and_moves_out_of_unset(env: Env, monkeypatch) -> None:
    sub = env.registry.add_subject("Anxiety", "journal")
    env.add("journal", "old entry", 1.0)
    win = env.browser()
    _select(win._owner_list, "journal")
    (note,) = win._visible_rows

    def fake_exec(self):
        assert self._subject_lbl.text() == "Subject: UNSET"  # current subject is displayed
        self._on_subject_changed(sub.id)
        self._title_edit.setText("A title")
        self._on_commit()
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(EditNoteDialog, "exec", fake_exec)
    win._on_edit_note(note)

    (latest,) = latest_revisions(env.repo.rows_for_owner("journal"))
    assert (latest.subject, latest.title, latest.text) == (sub.id, "A title", "old entry")
    _select(win._subject_list, "UNSET")
    assert _titles(win) == []
    _select(win._subject_list, "Anxiety")
    assert _titles(win) == ["A title"]


def test_edit_dialog_without_registry_keeps_subject(env: Env) -> None:
    keep = env.registry.add_subject("Keep", "journal")
    env.add("journal", "x", 1.0, subject=keep.id)
    (row,) = env.repo.rows_for_owner("journal")
    dlg = EditNoteDialog(row, env.writer)
    dlg._body_edit.setPlainText("changed")
    dlg._on_commit()
    (latest,) = latest_revisions(env.repo.rows_for_owner("journal"))
    assert (latest.text, latest.subject) == ("changed", keep.id)


def test_edit_dialog_files_the_revision_under_the_notes_own_owner(env: Env, monkeypatch) -> None:
    env.add("Prayer", "pray", 1.0)
    win = env.browser()
    _select(win._owner_list, "Prayer")
    (note,) = win._visible_rows
    monkeypatch.setattr(
        EditNoteDialog, "exec",
        lambda self: (self._body_edit.setPlainText("edited"), self._on_commit(),
                      QtWidgets.QDialog.DialogCode.Accepted)[-1],
    )
    win._on_edit_note(note)
    assert [r.revision_num for r in env.repo.rows_for_owner("Prayer")] == [1, 2]
    assert env.repo.rows_for_owner("journal") == []


# -- delete -------------------------------------------------------------------

def test_delete_confirmed_hides_note_but_keeps_its_history(env: Env, monkeypatch) -> None:
    env.add("journal", "keep", 1.0)
    env.add("journal", "drop", 2.0)
    win = env.browser()
    _select(win._owner_list, "journal")
    drop = next(n for n in win._visible_rows if n.text == "drop")
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.Yes),
    )
    win._on_delete_note(drop)
    assert _titles(win) == ["keep"]
    assert len(env.repo.rows_for_owner("journal")) == 3


def test_delete_declined_changes_nothing(env: Env, monkeypatch) -> None:
    env.add("journal", "keep", 1.0)
    win = env.browser()
    _select(win._owner_list, "journal")
    monkeypatch.setattr(
        QtWidgets.QMessageBox, "question",
        staticmethod(lambda *a, **k: QtWidgets.QMessageBox.StandardButton.No),
    )
    win._on_delete_note(win._visible_rows[0])
    assert len(env.repo.rows_for_owner("journal")) == 1


# -- new owner / new subject prompts -------------------------------------------

def test_new_owner_dialog_validates_live_and_creates(env: Env) -> None:
    dlg = NewOwnerDialog(env.registry)
    assert not dlg._ok.isEnabled()
    dlg._edit.setText("bad/name")
    assert not dlg._ok.isEnabled()
    dlg._edit.setText("Sleep Log")
    assert dlg._ok.isEnabled() and "sleep_log" in dlg._hint.text()
    dlg._on_accept()
    assert dlg.created.key == "sleep_log"
    assert "Sleep Log" in [o.label for o in env.registry.owners()]


def test_new_owner_dialog_reports_duplicates_without_closing(env: Env) -> None:
    dlg = NewOwnerDialog(env.registry)
    dlg._edit.setText("Journal")
    dlg._on_accept()
    assert dlg.created is None and dlg.result() != QtWidgets.QDialog.DialogCode.Accepted
    assert "already exists" in dlg._hint.text()


def test_new_subject_dialog_owner_or_all(env: Env) -> None:
    dlg = NewSubjectDialog(env.registry, env.registry.owners(), default_owner="Prayer")
    assert dlg._owner_combo.currentData() == "Prayer"
    dlg._edit.setText("Intercession")
    dlg._on_accept()
    assert dlg.created.owner == "Prayer"

    dlg2 = NewSubjectDialog(env.registry, env.registry.owners(), default_owner="Prayer")
    dlg2._owner_combo.setCurrentIndex(0)  # "All owners"
    dlg2._edit.setText("Everywhere")
    dlg2._on_accept()
    assert dlg2.created.owner is None


def test_browser_new_owner_and_subject_buttons_refresh_lists(env: Env, monkeypatch) -> None:
    win = env.browser()

    def owner_exec(self):
        self._edit.setText("Sleep Log")
        self._on_accept()
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(NewOwnerDialog, "exec", owner_exec)
    win._btn_new_owner.click()
    assert "Sleep Log" in _items(win._owner_list)
    assert win._owner_list.currentItem().text() == "Sleep Log"  # new owner is selected

    def subject_exec(self):
        self._edit.setText("Dreams")
        self._on_accept()
        return QtWidgets.QDialog.DialogCode.Accepted

    monkeypatch.setattr(NewSubjectDialog, "exec", subject_exec)
    win._btn_new_subject.click()
    assert _items(win._subject_list) == ["ALL", "UNSET", "Dreams"]
    assert win._subject_list.currentItem().text() == "Dreams"


def test_buttons_disabled_without_registry(env: Env) -> None:
    win = NotesBrowserWindow(env.repo, registry=None)
    assert not win._btn_new_owner.isEnabled() and not win._btn_new_subject.isEnabled()


# -- subject picker popup ------------------------------------------------------

def test_subject_picker_lists_owner_subjects_and_selects_one(env: Env) -> None:
    a = env.registry.add_subject("Alpha", "journal")
    env.registry.add_subject("PrayerOnly", "Prayer")
    picked: list[str] = []
    pop = SubjectPickerPopup(env.registry, "journal", UNSET_SUBJECT, picked.append)
    labels = [b.text() for b in pop._group.buttons()]
    assert labels == ["UNSET", "Alpha"]
    next(b for b in pop._group.buttons() if b.text() == "Alpha").setChecked(True)
    assert picked == [a.id]


def test_subject_picker_creates_new_subject_for_owner(env: Env) -> None:
    picked: list[str] = []
    pop = SubjectPickerPopup(env.registry, "journal", UNSET_SUBJECT, picked.append)
    pop._edit.setText("Fresh")
    pop._add_new_subject()
    (created,) = env.registry.subjects_for_owner("journal")
    assert created.label == "Fresh" and created.owner == "journal"
    assert picked == [created.id]
    pop._edit.setText("fresh")  # duplicate -> inline error, nothing new
    pop._add_new_subject()
    assert pop._error.isVisibleTo(pop) and len(env.registry.subjects()) == 1
