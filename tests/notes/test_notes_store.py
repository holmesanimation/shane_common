"""NotesStore: schema, revision chain, conflicts, integrity, crash safety, threads."""

from __future__ import annotations

import dataclasses
import sqlite3
import threading
from pathlib import Path

import pytest

from shane_common.notes.db import SCHEMA_VERSION, NotesDb, NotesDbError
from shane_common.notes.model import (
    OP_DELETE,
    OP_EDIT,
    UNSET_SUBJECT,
    Note,
    NoteCorrelation,
    NoteType,
    make_note_id,
)
from shane_common.notes.notes_writer import NotesWriter
from shane_common.notes.revisions import commit_revision
from shane_common.notes.store import (
    NoteConflictError,
    NoteNotFoundError,
    NotesStore,
    NotesStoreError,
    UnknownSubjectError,
)


@pytest.fixture
def store(tmp_path: Path):
    s = NotesStore(tmp_path / "data" / "notes.db")
    yield s
    s.close()


def _writer(store: NotesStore, owner: str = "journal") -> NotesWriter:
    return NotesWriter(store, owner)


def _add(store: NotesStore, text: str = "hello", owner: str = "journal", **kw) -> Note:
    w = _writer(store, owner)
    note = w.build_note(note_type=NoteType.GENERAL, text=text, **kw)
    w.commit(note)
    return note


# -- creation / schema ---------------------------------------------------------

def test_constructing_a_store_does_not_touch_the_disk(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "notes.db"
    s = NotesStore(path)
    assert not path.exists() and not path.parent.exists()
    s.owners()  # first use creates and migrates
    assert path.exists()
    s.close()


def test_new_database_has_schema_version_and_wal_and_foreign_keys(store: NotesStore) -> None:
    assert store.schema_version() == SCHEMA_VERSION
    conn = store._db.connection()
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL


def test_newer_schema_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "notes.db"
    s = NotesStore(path)
    s.owners()
    s.close()
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(SCHEMA_VERSION + 1),))
    conn.commit()
    conn.close()
    with pytest.raises(NotesDbError):
        NotesStore(path).owners()


def test_write_seq_increases_once_per_write_transaction(store: NotesStore) -> None:
    before = store.write_seq()
    _add(store, "a")
    assert store.write_seq() == before + 1
    store.rows_for_owner("journal")  # reads do not bump it
    assert store.write_seq() == before + 1


def test_builtin_owners_are_registered_on_first_open(tmp_path: Path) -> None:
    s = NotesStore(tmp_path / "notes.db")
    s.declare_builtin_owners([("journal", "Journal"), ("Prayer", "Prayer")])
    assert not (tmp_path / "notes.db").exists()  # still lazy
    assert [(o.key, o.label, o.builtin) for o in s.owners()] == [
        ("journal", "Journal", True),
        ("Prayer", "Prayer", True),
    ]
    s.close()


# -- revision chain --------------------------------------------------------------

def test_new_note_has_real_id_and_revision_one(store: NotesStore) -> None:
    note = _add(store, "hello")
    (row,) = store.rows_for_owner("journal")
    assert row.note_id == note.note_id and row.note_id.startswith("note_")
    assert (row.revision_num, row.op, row.owner_key) == (1, "create", "journal")


def test_edit_and_delete_append_revisions_and_move_the_head(store: NotesStore) -> None:
    note = _add(store, "v1")
    w = _writer(store)
    (row,) = store.rows_for_owner("journal")
    commit_revision(w, row, op=OP_EDIT, text="v2", title="T", subject=UNSET_SUBJECT)
    latest = store.latest_revision(note.note_id)
    assert (latest.revision_num, latest.text, latest.title) == (2, "v2", "T")
    commit_revision(w, latest, op=OP_DELETE, text=latest.text, title=latest.title, subject=UNSET_SUBJECT)

    assert [r.revision_num for r in store.note_history(note.note_id)] == [1, 2, 3]
    assert store.latest_notes_for_owner("journal") == []
    (tomb,) = store.latest_notes_for_owner("journal", include_deleted=True)
    assert tomb.is_deleted and tomb.text == "v2"


def test_edit_conflict_is_reported_not_interleaved(store: NotesStore) -> None:
    note = _add(store, "v1")
    w = _writer(store)
    stale = store.latest_revision(note.note_id)
    commit_revision(w, stale, op=OP_EDIT, text="first", title=None, subject=UNSET_SUBJECT)
    with pytest.raises(NoteConflictError):
        commit_revision(w, stale, op=OP_EDIT, text="second", title=None, subject=UNSET_SUBJECT)
    assert store.latest_revision(note.note_id).text == "first"
    assert len(store.note_history(note.note_id)) == 2


def test_revision_gaps_and_unknown_notes_are_rejected(store: NotesStore) -> None:
    note = _add(store, "v1")
    w = _writer(store)
    gap = w.build_note(text="x", note_id=note.note_id, revision_num=3, op=OP_EDIT)
    with pytest.raises(NoteConflictError):
        w.commit(gap)
    ghost = w.build_note(text="x", note_id="note_missing", revision_num=2, op=OP_EDIT)
    with pytest.raises(NoteNotFoundError):
        w.commit(ghost)


def test_creating_the_same_note_twice_conflicts(store: NotesStore) -> None:
    note = _add(store, "once")
    with pytest.raises(NoteConflictError):
        _writer(store).commit(note)


def test_edit_cannot_move_a_note_to_another_owner(store: NotesStore) -> None:
    note = _add(store, "mine", owner="journal")
    other = _writer(store, "Prayer")
    with pytest.raises(NotesStoreError):
        other.commit(other.build_note(text="x", note_id=note.note_id, revision_num=2, op=OP_EDIT))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"op": OP_EDIT},                      # revision 1 must be 'create'
        {"revision_num": 2},                  # later revisions cannot be 'create'
        {"revision_num": 0},
        {"op": "bogus", "revision_num": 2},
    ],
)
def test_malformed_revision_arguments_raise_value_error(store: NotesStore, kwargs) -> None:
    w = _writer(store)
    with pytest.raises(ValueError):
        store.add_revision(w.build_note(text="x", **kwargs))


def test_unknown_owner_is_registered_on_first_note(store: NotesStore) -> None:
    _add(store, "x", owner="adhoc")
    assert "adhoc" in [o.key for o in store.owners()]


# -- integrity -------------------------------------------------------------------

def test_unknown_subject_is_rejected_and_writes_nothing(store: NotesStore) -> None:
    w = _writer(store)
    with pytest.raises(UnknownSubjectError):
        w.commit(w.build_note(text="x", subject="subj_nope"))
    assert store.rows_for_owner("journal") == []
    assert store.list_owner_keys() == []


def test_subject_round_trips_and_unset_is_null(store: NotesStore) -> None:
    sub = store.add_subject("subj_1", "Anxiety", None)
    _add(store, "with", subject=sub.id)
    _add(store, "without")
    by_text = {r.text: r for r in store.rows_for_owner("journal")}
    assert by_text["with"].subject == "subj_1" and by_text["without"].subject == UNSET_SUBJECT
    raw = store._db.connection().execute("SELECT subject_id FROM revisions ORDER BY rowid").fetchall()
    assert [r[0] for r in raw] == ["subj_1", None]


def test_foreign_key_check_is_clean_after_normal_use(store: NotesStore) -> None:
    sub = store.add_subject("subj_1", "S", None)
    n = _add(store, "a", subject=sub.id)
    _writer(store).commit_correlation(NoteCorrelation("c1", [n.note_id], [], "", "x", "", [], 1.0))
    assert store._db.connection().execute("PRAGMA foreign_key_check").fetchall() == []
    assert store._db.connection().execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_subject_label_unique_per_owner_case_insensitively(store: NotesStore) -> None:
    store.add_owner("journal", "journal")
    store.add_subject("s1", "Ideas", "journal")
    with pytest.raises(NotesStoreError):
        store.add_subject("s2", "IDEAS", "journal")


# -- crash safety ----------------------------------------------------------------

def test_write_aborted_mid_transaction_leaves_nothing_partial(store: NotesStore) -> None:
    note = _add(store, "v1")
    seq = store.write_seq()
    w = _writer(store)
    stale = store.latest_revision(note.note_id)

    conn = store._db.connection()
    # Fail the head-pointer UPDATE, i.e. *after* the revision INSERT already succeeded.
    conn.execute(
        "CREATE TEMP TRIGGER boom BEFORE UPDATE ON notes BEGIN SELECT RAISE(ABORT, 'boom'); END"
    )
    with pytest.raises(sqlite3.Error):
        commit_revision(w, stale, op=OP_EDIT, text="v2", title=None, subject=UNSET_SUBJECT)
    conn.execute("DROP TRIGGER boom")

    assert [r.revision_num for r in store.note_history(note.note_id)] == [1]  # insert rolled back
    assert store.latest_revision(note.note_id).text == "v1"
    assert store.write_seq() == seq  # nothing committed, nothing counted
    assert conn.in_transaction is False

def test_committed_data_survives_reopening(tmp_path: Path) -> None:
    path = tmp_path / "notes.db"
    s1 = NotesStore(path)
    note = _add(s1, "durable")
    s1.close()
    s2 = NotesStore(path)
    assert [r.note_id for r in s2.rows_for_owner("journal")] == [note.note_id]
    s2.close()


# -- concurrency -------------------------------------------------------------------

def test_each_thread_gets_its_own_connection_and_writers_do_not_collide(store: NotesStore) -> None:
    store.owners()
    errors: list[BaseException] = []

    def work(idx: int) -> None:
        try:
            for i in range(20):
                _add(store, f"t{idx}-{i}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(store.rows_for_owner("journal")) == 80
    assert len({id(c) for c in store._db._all}) >= 2


def test_reader_sees_committed_data_while_another_connection_holds_a_read_snapshot(store: NotesStore) -> None:
    _add(store, "first")
    reader = sqlite3.connect(store.path, isolation_level=None)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM revisions").fetchone()  # pins a WAL snapshot
    _add(store, "second")  # writers are not blocked by readers in WAL mode
    assert reader.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1
    reader.execute("COMMIT")
    assert reader.execute("SELECT count(*) FROM revisions").fetchone()[0] == 2
    reader.close()


# -- queries -----------------------------------------------------------------------

def test_row_queries_use_head_revisions_only(store: NotesStore) -> None:
    w = _writer(store)
    n1 = w.build_note(text="one", table_id="t", row_id="r1", row_snapshot={"k": 1}, row_ts=5.0,
                      owner_module="m", owner_class="C", table_label="T")
    n2 = w.build_note(text="two", table_id="t", row_id="r1")
    n3 = w.build_note(text="other", table_id="t", row_id="r2")
    for n in (n1, n2, n3):
        w.commit(n)
    (first,) = [r for r in store.latest_notes_for_row("r1") if r.text == "one"]
    assert (first.table_id, first.row_snapshot, first.row_ts, first.owner_module) == ("t", {"k": 1}, 5.0, "m")
    assert store.row_id_to_note_count("journal") == {"r1": 2, "r2": 1}

    commit_revision(w, first, op=OP_EDIT, text="one!", title=None, subject=UNSET_SUBJECT)
    edited = store.latest_notes_for_row("r1")
    assert sorted(r.text for r in edited) == ["one!", "two"]
    assert len(store.rows_for_row_id("r1")) == 3  # every revision is still there

    commit_revision(w, edited[0] if edited[0].text == "one!" else edited[1], op=OP_DELETE,
                    text="one!", title=None, subject=UNSET_SUBJECT)
    assert store.row_id_to_note_count("journal") == {"r1": 1, "r2": 1}


def test_latest_notes_order_and_limits(store: NotesStore) -> None:
    w = _writer(store)
    for ts, text in ((3.0, "c"), (1.0, "a"), (2.0, "b")):
        n = w.build_note(text=text)
        w.commit(dataclasses.replace(n, ts=ts))
    assert [r.text for r in store.latest_notes_for_owner("journal")] == ["a", "b", "c"]
    assert [r.text for r in store.latest_notes_for_owner("journal", newest_first=True)] == ["c", "b", "a"]
    assert [r.text for r in store.recent_rows(2)] == ["c", "b"]


# -- correlations ------------------------------------------------------------------

def test_correlation_round_trip_and_lookup(store: NotesStore) -> None:
    a, b = _add(store, "a"), _add(store, "b")
    w = _writer(store)
    corr = w.build_correlation(note_ids=[a.note_id, b.note_id], row_ids=["r1"], relation_type="sequence",
                               hypothesis="a then b", tags=["x"])
    w.commit_correlation(corr)
    (row,) = store.all_correlations()
    assert (row.correlation_id, row.note_ids, row.row_ids, row.tags) == (
        corr.correlation_id, [a.note_id, b.note_id], ["r1"], ["x"],
    )
    assert store.correlations_for_note_ids([a.note_id])[0].hypothesis == "a then b"
    assert store.correlations_for_note_ids(["nope"]) == []


def test_correlation_with_unknown_note_is_rejected_atomically(store: NotesStore) -> None:
    a = _add(store, "a")
    w = _writer(store)
    with pytest.raises(NotesStoreError):
        w.commit_correlation(w.build_correlation(note_ids=[a.note_id, "note_missing"]))
    assert store.all_correlations() == []


# -- read-only access ----------------------------------------------------------------

def test_open_readonly_reads_but_cannot_write(tmp_path: Path) -> None:
    path = tmp_path / "notes.db"
    s = NotesStore(path)
    note = _add(s, "x")
    s.close()

    ro = NotesStore.open_readonly(path)
    assert [r.note_id for r in ro.recent_rows()] == [note.note_id]
    with pytest.raises(NotesDbError):
        _add(ro, "y")
    ro.close()


def test_open_readonly_on_missing_file_fails_cleanly(tmp_path: Path) -> None:
    with pytest.raises(NotesDbError):
        NotesStore.open_readonly(tmp_path / "missing.db").owners()
    assert not (tmp_path / "missing.db").exists()


def test_make_note_id_is_utc_and_sortable() -> None:
    assert make_note_id(0.0).startswith("note_19700101T000000000Z_")
    assert make_note_id(1.0) > make_note_id(0.0)
