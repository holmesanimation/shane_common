"""Title / subject fields, edit and delete revisions, table-row notes."""

from __future__ import annotations

from pathlib import Path

import pytest

from shane_common.notes.notes_repository import NotesRepository, latest_revisions
from shane_common.notes.notes_writer import (
    OP_DELETE,
    OP_EDIT,
    UNSET_SUBJECT,
    NotesWriter,
    NoteType,
)
from shane_common.notes.revisions import commit_revision
from shane_common.notes.store import NotesStore


@pytest.fixture
def store(tmp_path: Path):
    s = NotesStore(tmp_path / "notes.db")
    yield s
    s.close()


def _writer(store: NotesStore, owner: str = "journal") -> NotesWriter:
    return NotesWriter(store, owner)


def _subject(store: NotesStore, sid: str, owner: str | None = None) -> str:
    store.add_subject(sid, sid, owner)
    return sid


# ---------------------------------------------------------------------------
# New fields round-trip
# ---------------------------------------------------------------------------

def test_new_note_defaults_title_none_subject_unset(store: NotesStore) -> None:
    w = _writer(store)
    w.commit(w.build_note(note_type=NoteType.GENERAL, text="hello"))

    (row,) = NotesRepository(store).rows_for_owner("journal")
    assert row.title is None
    assert row.subject == UNSET_SUBJECT


def test_title_and_subject_round_trip(store: NotesStore) -> None:
    sid = _subject(store, "subj_1234abcd")
    w = _writer(store)
    w.commit(w.build_note(note_type=NoteType.GENERAL, text="hello", title="  My title  ", subject=sid))

    (row,) = NotesRepository(store).rows_for_owner("journal")
    assert (row.title, row.subject) == ("My title", "subj_1234abcd")


def test_blank_title_is_stored_as_null(store: NotesStore) -> None:
    w = _writer(store)
    w.commit(w.build_note(note_type=NoteType.GENERAL, text="t", title="   "))
    raw = store._db.connection().execute("SELECT title FROM revisions").fetchone()
    assert raw[0] is None


def test_blank_or_missing_subject_reads_as_unset(store: NotesStore) -> None:
    w = _writer(store)
    for subject in (None, "", "  ", UNSET_SUBJECT):
        w.commit(w.build_note(note_type=NoteType.GENERAL, text="x", subject=subject))
    rows = NotesRepository(store).rows_for_owner("journal")
    assert [r.subject for r in rows] == [UNSET_SUBJECT] * 4


def test_table_note_carries_title_subject_and_row_link(store: NotesStore) -> None:
    sid = _subject(store, "subj_a")
    w = _writer(store)
    w.commit(
        w.build_note(
            owner_module="m", owner_class="C", table_id="t", table_label="T", row_id="r1",
            text="x", title="T", subject=sid,
        )
    )
    (row,) = NotesRepository(store).rows_for_owner("journal")
    assert (row.title, row.subject, row.table_id, row.row_id) == ("T", "subj_a", "t", "r1")


# ---------------------------------------------------------------------------
# Edit = new revision
# ---------------------------------------------------------------------------

def test_editing_a_note_appends_revision_two_and_collapses(store: NotesStore) -> None:
    sid = _subject(store, "subj_a")
    w = _writer(store)
    w.commit(w.build_note(text="old entry", context={"tags": ["x"]}))
    repo = NotesRepository(store)
    (row,) = repo.rows_for_owner("journal")

    committed = commit_revision(w, row, op=OP_EDIT, text="new text", title="T", subject=sid)

    assert committed.revision_num == 2
    assert len(repo.rows_for_owner("journal")) == 2  # append-only: old revision still stored
    (latest,) = latest_revisions(repo.rows_for_owner("journal"))
    assert latest.text == "new text"
    assert (latest.title, latest.subject) == ("T", "subj_a")
    assert latest.context["tags"] == ["x"]  # unrelated context preserved
    assert latest.note_id == row.note_id
    assert latest.revision_num == 2
    (head,) = repo.latest_notes_for_owner("journal")
    assert head.text == "new text"


def test_editing_twice_increments_revision(store: NotesStore) -> None:
    w = _writer(store)
    w.commit(w.build_note(text="old"))
    repo = NotesRepository(store)
    for text in ("one", "two"):
        (row,) = repo.latest_notes_for_owner("journal")
        commit_revision(w, row, op=OP_EDIT, text=text, title=None, subject=UNSET_SUBJECT)
    (latest,) = repo.latest_notes_for_owner("journal")
    assert (latest.text, latest.revision_num) == ("two", 3)


def test_edit_keeps_context_updates_merged(store: NotesStore) -> None:
    w = _writer(store)
    w.commit(w.build_note(text="a", context={"calories": 100, "tags": ["t"]}))
    (row,) = NotesRepository(store).rows_for_owner("journal")
    commit_revision(w, row, op=OP_EDIT, text="a", title=None, subject=UNSET_SUBJECT,
                    context_updates={"calories": None})
    (latest,) = NotesRepository(store).latest_notes_for_owner("journal")
    assert latest.context == {"calories": None, "tags": ["t"]}


# ---------------------------------------------------------------------------
# Delete = tombstone revision
# ---------------------------------------------------------------------------

def test_delete_hides_note_but_keeps_history(store: NotesStore) -> None:
    w = _writer(store)
    w.commit(w.build_note(text="doomed"))
    w.commit(w.build_note(text="keeper"))
    repo = NotesRepository(store)
    doomed = next(r for r in repo.rows_for_owner("journal") if r.text == "doomed")

    commit_revision(w, doomed, op=OP_DELETE, text=doomed.text, title=None, subject=UNSET_SUBJECT)

    assert [r.text for r in latest_revisions(repo.rows_for_owner("journal"))] == ["keeper"]
    assert [r.text for r in repo.latest_notes_for_owner("journal")] == ["keeper"]
    assert len(repo.rows_for_owner("journal")) == 3  # nothing removed
    assert {r.text for r in latest_revisions(repo.rows_for_owner("journal"), include_deleted=True)} == {
        "doomed", "keeper",
    }
    assert {r.text for r in repo.latest_notes_for_owner("journal", include_deleted=True)} == {
        "doomed", "keeper",
    }


def test_edit_and_delete_of_table_note_keep_table_metadata(store: NotesStore) -> None:
    sid = _subject(store, "subj_z")
    w = _writer(store)
    w.commit(
        w.build_note(
            owner_module="m", owner_class="C", table_id="tbl", table_label="Tbl",
            row_id="row-9", row_snapshot={"k": 1}, text="table text",
        )
    )
    repo = NotesRepository(store)
    (row,) = repo.rows_for_owner("journal")

    commit_revision(w, row, op=OP_EDIT, text="edited", title="t", subject=sid)
    (latest,) = repo.latest_notes_for_row("row-9")
    assert latest.revision_num == 2 and latest.op == OP_EDIT
    assert (latest.table_id, latest.row_snapshot, latest.subject) == ("tbl", {"k": 1}, "subj_z")

    commit_revision(w, latest, op=OP_DELETE, text=latest.text, title=latest.title, subject=latest.subject)
    assert repo.latest_notes_for_row("row-9") == []
    assert repo.row_id_to_note_count("journal") == {}


def test_for_owner_shares_the_store_and_files_under_that_owner(store: NotesStore) -> None:
    base = _writer(store, "journal")
    other = base.for_owner("Prayer")
    assert other.store is base.store and other.owner == "Prayer" and base.for_owner("journal") is base
    other.commit(other.build_note(text="p"))
    repo = NotesRepository(store)
    assert [r.text for r in repo.rows_for_owner("Prayer")] == ["p"]
    assert repo.rows_for_owner("journal") == []
    assert repo.list_owners() == ["Prayer"]
