# shane_common/notes/store.py
"""``NotesStore`` — the SQLite source of truth for notes, owners, subjects and correlations.

Storage is append-only at the note level: creating, editing and deleting a note
each insert a row into ``revisions``; the only in-place update is the small
"head pointer" on ``notes`` (``head_rev`` / ``deleted``).  Every write is one
transaction, so a failed write leaves nothing partial behind.

Two writers racing on the same revision collide on ``revisions``'s primary key
(or on the head-pointer check) and the loser gets a :class:`NoteConflictError`
instead of silently interleaving.

Subjects are stored as ``subject_id`` (``NULL`` = ``UNSET``); the API value for
"no subject" is the string ``"UNSET"``.

No Qt dependencies.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from shane_common.notes.db import NotesDb, SCHEMA_VERSION, read_schema_version
from shane_common.notes.model import (
    OP_CREATE,
    OP_DELETE,
    OP_EDIT,
    UNSET_SUBJECT,
    CorrelationRow,
    Note,
    NoteCorrelation,
    NoteRow,
    clean_subject,
    clean_title,
    note_row_from_note,
)


class NotesStoreError(Exception):
    """A write was rejected.  The message is written for end users."""


class NoteConflictError(NotesStoreError):
    """The note was changed by someone else since it was read."""


class NoteNotFoundError(NotesStoreError):
    """An edit or delete referred to a note that does not exist."""


class UnknownSubjectError(NotesStoreError):
    """A note referred to a subject that is not in the subjects table."""


@dataclass(frozen=True)
class OwnerRecord:
    key: str
    label: str
    builtin: bool = False


@dataclass(frozen=True)
class SubjectRecord:
    id: str
    label: str
    owner_key: str | None  # ``None`` = available to all owners


_REVISION_COLUMNS = """
    r.note_id, r.revision_num, r.op, r.ts, r.wall_ts, r.note_type, r.title,
    r.subject_id, r.text, r.instrument, r.strategy_id, r.context,
    r.table_id, r.table_label, r.row_id, r.row_ts, r.row_snapshot,
    r.owner_module, r.owner_class, n.owner_key AS owner_key
"""
_ALL_REVISIONS = (
    f"SELECT {_REVISION_COLUMNS} FROM revisions r JOIN notes n ON n.note_id = r.note_id"
)
_HEAD_REVISIONS = (
    f"SELECT {_REVISION_COLUMNS} FROM notes n "
    "JOIN revisions r ON r.note_id = n.note_id AND r.revision_num = n.head_rev"
)


def _loads(text: str | None, default: Any) -> Any:
    if not text:
        return default
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return default
    return value if isinstance(value, type(default)) else default


def _to_note_row(r: sqlite3.Row) -> NoteRow:
    return NoteRow(
        ts=r["ts"],
        wall_ts=r["wall_ts"],
        note_type=r["note_type"],
        text=r["text"] or "",
        instrument=r["instrument"],
        strategy_id=r["strategy_id"],
        context=_loads(r["context"], {}),
        title=r["title"],
        subject=r["subject_id"] or UNSET_SUBJECT,
        note_id=r["note_id"],
        revision_num=r["revision_num"],
        op=r["op"],
        owner_key=r["owner_key"],
        owner_module=r["owner_module"],
        owner_class=r["owner_class"],
        table_id=r["table_id"],
        table_label=r["table_label"],
        row_id=r["row_id"],
        row_ts=r["row_ts"],
        row_snapshot=_loads(r["row_snapshot"], {}),
    )


class NotesStore:
    """Notes, owners, subjects and correlations in one SQLite file.

    Constructing a store does not touch the disk; the file (and its parent
    directory) is created and migrated on first use.

    Parameters
    ----------
    path:
        Database file (conventionally ``notes.db``).
    readonly:
        Open an existing database read-only, without migrating it.  See
        :meth:`open_readonly`.
    """

    def __init__(self, path: str | Path, *, readonly: bool = False) -> None:
        self._builtin_owners: tuple[OwnerRecord, ...] = ()
        self._db = NotesDb(path, readonly=readonly, on_open=self._register_builtin_owners)

    @classmethod
    def open_readonly(cls, path: str | Path) -> "NotesStore":
        """Open an existing database (e.g. a restored snapshot) for reading only."""
        return cls(path, readonly=True)

    @property
    def path(self) -> Path:
        return self._db.path

    def close(self) -> None:
        self._db.close()

    def transaction(self):
        """Context manager for a write transaction (nests; see ``NotesDb.transaction``)."""
        return self._db.transaction()

    def write_seq(self) -> int:
        return self._db.write_seq()

    def schema_version(self) -> int:
        return read_schema_version(self._db.connection())

    # ------------------------------------------------------------------
    # Owners
    # ------------------------------------------------------------------

    def declare_builtin_owners(self, owners: Iterable[tuple[str, str]]) -> None:
        """Register hardcoded ``(key, label)`` owners; they are written when the DB opens."""
        self._builtin_owners = tuple(
            OwnerRecord(key=str(k), label=str(lbl), builtin=True) for k, lbl in owners
        )
        if self._db.is_open and not self._db.readonly:
            self._register_builtin_owners(self._db.connection())

    def _register_builtin_owners(self, conn: sqlite3.Connection) -> None:
        if not self._builtin_owners:
            return
        existing = {
            r["key"]: (r["label"], r["builtin"])
            for r in conn.execute("SELECT key, label, builtin FROM owners")
        }
        pending = [o for o in self._builtin_owners if existing.get(o.key) != (o.label, 1)]
        if not pending:
            return
        conn.execute("BEGIN IMMEDIATE")
        try:
            for o in pending:
                conn.execute(
                    "INSERT INTO owners(key, label, builtin) VALUES (?, ?, 1) "
                    "ON CONFLICT(key) DO UPDATE SET label = excluded.label, builtin = 1",
                    (o.key, o.label),
                )
            conn.execute(
                "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'write_seq'"
            )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def owners(self) -> list[OwnerRecord]:
        """Every owner: built-in first (in declared order), then the rest A-Z by label."""
        rows = self._db.connection().execute(
            "SELECT key, label, builtin FROM owners"
        ).fetchall()
        records = {r["key"]: OwnerRecord(r["key"], r["label"], bool(r["builtin"])) for r in rows}
        ordered: list[OwnerRecord] = []
        for declared in self._builtin_owners:
            if declared.key in records:
                ordered.append(records.pop(declared.key))
        ordered.extend(o for o in records.values() if o.builtin)
        for o in list(ordered):
            records.pop(o.key, None)
        ordered.extend(sorted(records.values(), key=lambda o: o.label.casefold()))
        return ordered

    def add_owner(self, key: str, label: str, *, builtin: bool = False) -> OwnerRecord:
        try:
            with self.transaction() as conn:
                conn.execute(
                    "INSERT INTO owners(key, label, builtin) VALUES (?, ?, ?)",
                    (key, label, int(builtin)),
                )
        except sqlite3.IntegrityError as exc:
            raise NotesStoreError(f"An owner with the key “{key}” already exists.") from exc
        return OwnerRecord(key, label, builtin)

    # ------------------------------------------------------------------
    # Subjects
    # ------------------------------------------------------------------

    def subjects(self) -> list[SubjectRecord]:
        rows = self._db.connection().execute(
            "SELECT id, label, owner_key FROM subjects ORDER BY rowid"
        ).fetchall()
        return [SubjectRecord(r["id"], r["label"], r["owner_key"]) for r in rows]

    def add_subject(self, subject_id: str, label: str, owner_key: str | None) -> SubjectRecord:
        try:
            with self.transaction() as conn:
                conn.execute(
                    "INSERT INTO subjects(id, label, owner_key) VALUES (?, ?, ?)",
                    (subject_id, label, owner_key),
                )
        except sqlite3.IntegrityError as exc:
            raise NotesStoreError(f"Could not add the subject “{label}”: {exc}") from exc
        return SubjectRecord(subject_id, label, owner_key)

    # ------------------------------------------------------------------
    # Writing notes
    # ------------------------------------------------------------------

    def add_revision(self, note: Note) -> NoteRow:
        """Append one revision (create, edit or delete) of a note, atomically.

        The owner is registered on the fly if it is not known yet.  Raises
        :class:`NoteConflictError` if *note*'s revision is not the successor of
        the note's current head, :class:`NoteNotFoundError` for an edit of a
        missing note and :class:`UnknownSubjectError` for an unknown subject.
        """
        note_id = str(note.note_id or "").strip()
        if not note_id:
            raise ValueError("a note revision needs a note_id")
        if note.op not in (OP_CREATE, OP_EDIT, OP_DELETE):
            raise ValueError(f"unsupported revision op: {note.op!r}")
        revision_num = int(note.revision_num)
        if revision_num < 1:
            raise ValueError("revision_num starts at 1")
        if (revision_num == 1) != (note.op == OP_CREATE):
            raise ValueError("revision 1 must be op 'create', later revisions 'edit' or 'delete'")
        owner = str(note.owner).strip()
        if not owner:
            raise ValueError("a note needs an owner")

        title = clean_title(note.title)
        subject = clean_subject(note.subject)
        subject_id = None if subject == UNSET_SUBJECT else subject
        context = json.dumps(dict(note.context or {}), default=str)
        snapshot = json.dumps(dict(note.row_snapshot), default=str) if note.row_snapshot else None

        with self.transaction() as conn:
            if subject_id is not None and conn.execute(
                "SELECT 1 FROM subjects WHERE id = ?", (subject_id,)
            ).fetchone() is None:
                raise UnknownSubjectError(f"Unknown subject “{subject_id}”.")

            if revision_num == 1:
                conn.execute(
                    "INSERT OR IGNORE INTO owners(key, label, builtin) VALUES (?, ?, 0)",
                    (owner, owner),
                )
                try:
                    conn.execute(
                        "INSERT INTO notes(note_id, owner_key, created_ts, head_rev, deleted) "
                        "VALUES (?, ?, ?, 1, 0)",
                        (note_id, owner, float(note.ts)),
                    )
                except sqlite3.IntegrityError as exc:
                    raise NoteConflictError(f"Note {note_id} already exists.") from exc
            else:
                head = conn.execute(
                    "SELECT owner_key, head_rev FROM notes WHERE note_id = ?", (note_id,)
                ).fetchone()
                if head is None:
                    raise NoteNotFoundError(f"Note {note_id} does not exist.")
                if head["owner_key"] != owner:
                    raise NotesStoreError(
                        f"Note {note_id} belongs to “{head['owner_key']}”, not “{owner}”."
                    )
                if head["head_rev"] != revision_num - 1:
                    raise NoteConflictError(
                        "This note was changed elsewhere since it was loaded. "
                        "Refresh and try again."
                    )

            try:
                conn.execute(
                    "INSERT INTO revisions(note_id, revision_num, op, ts, wall_ts, note_type, "
                    "title, subject_id, text, instrument, strategy_id, context, table_id, "
                    "table_label, row_id, row_ts, row_snapshot, owner_module, owner_class) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        note_id, revision_num, note.op, float(note.ts), float(note.wall_ts),
                        str(note.note_type), title, subject_id, str(note.text),
                        note.instrument, note.strategy_id, context,
                        note.table_id, note.table_label, note.row_id or None,
                        note.row_ts, snapshot, note.owner_module, note.owner_class,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise NoteConflictError(
                    "This note was changed elsewhere since it was loaded. Refresh and try again."
                ) from exc
            conn.execute(
                "UPDATE notes SET head_rev = ?, deleted = ? WHERE note_id = ?",
                (revision_num, int(note.op == OP_DELETE), note_id),
            )

        return note_row_from_note(note)

    # ------------------------------------------------------------------
    # Reading notes
    # ------------------------------------------------------------------

    def _query(self, sql: str, params: tuple = ()) -> list[NoteRow]:
        rows = self._db.connection().execute(sql, params).fetchall()
        return [_to_note_row(r) for r in rows]

    def list_owner_keys(self) -> list[str]:
        """Distinct owner keys that have at least one note, sorted."""
        rows = self._db.connection().execute(
            "SELECT DISTINCT owner_key FROM notes ORDER BY owner_key"
        ).fetchall()
        return [r["owner_key"] for r in rows]

    def all_rows(self) -> list[NoteRow]:
        """Every stored revision of every note, in write order."""
        return self._query(f"{_ALL_REVISIONS} ORDER BY r.rowid")

    def rows_for_owner(self, owner_key: str) -> list[NoteRow]:
        """Every revision of every note owned by *owner_key*, in write order."""
        return self._query(
            f"{_ALL_REVISIONS} WHERE n.owner_key = ? ORDER BY r.rowid", (owner_key,)
        )

    def note_history(self, note_id: str) -> list[NoteRow]:
        """Every revision of one note, oldest first."""
        return self._query(
            f"{_ALL_REVISIONS} WHERE r.note_id = ? ORDER BY r.revision_num", (note_id,)
        )

    def latest_revision(self, note_id: str) -> NoteRow | None:
        """The head revision of *note_id* (a delete tombstone if it was deleted)."""
        rows = self._query(f"{_HEAD_REVISIONS} WHERE n.note_id = ?", (note_id,))
        return rows[0] if rows else None

    def latest_notes_for_owner(
        self, owner_key: str, *, include_deleted: bool = False, newest_first: bool = False
    ) -> list[NoteRow]:
        """Head revision of each of *owner_key*'s notes (deleted ones omitted by default)."""
        where = "WHERE n.owner_key = ?" + ("" if include_deleted else " AND n.deleted = 0")
        order = "DESC" if newest_first else "ASC"
        return self._query(
            f"{_HEAD_REVISIONS} {where} ORDER BY r.ts {order}, r.rowid {order}", (owner_key,)
        )

    def recent_rows(self, limit: int = 25) -> list[NoteRow]:
        """The newest live notes across all owners (head revisions)."""
        return self._query(
            f"{_HEAD_REVISIONS} WHERE n.deleted = 0 ORDER BY r.ts DESC, r.rowid DESC LIMIT ?",
            (int(limit),),
        )

    def rows_for_row_id(self, row_id: str) -> list[NoteRow]:
        """Every revision attached to a table ``row_id``."""
        return self._query(
            f"{_ALL_REVISIONS} WHERE r.row_id = ? ORDER BY r.rowid", (row_id,)
        )

    def latest_notes_for_row(self, row_id: str) -> list[NoteRow]:
        """Head revision of each live note attached to ``row_id``, oldest first."""
        return self._query(
            f"{_HEAD_REVISIONS} WHERE r.row_id = ? AND n.deleted = 0 ORDER BY r.ts, r.rowid",
            (row_id,),
        )

    def row_id_to_note_count(self, owner_key: str) -> dict[str, int]:
        """``{row_id: number of live notes}`` for *owner_key*."""
        rows = self._db.connection().execute(
            "SELECT r.row_id AS row_id, COUNT(*) AS n FROM notes n "
            "JOIN revisions r ON r.note_id = n.note_id AND r.revision_num = n.head_rev "
            "WHERE n.owner_key = ? AND n.deleted = 0 AND r.row_id IS NOT NULL "
            "GROUP BY r.row_id",
            (owner_key,),
        ).fetchall()
        return {r["row_id"]: r["n"] for r in rows}

    # ------------------------------------------------------------------
    # Correlations
    # ------------------------------------------------------------------

    def add_correlation(self, correlation: NoteCorrelation) -> None:
        note_ids = list(dict.fromkeys(str(n) for n in correlation.note_ids))
        try:
            with self.transaction() as conn:
                conn.execute(
                    "INSERT INTO correlations(correlation_id, time_window, relation_type, "
                    "hypothesis, tags, row_ids, created_ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        correlation.correlation_id,
                        correlation.time_window,
                        correlation.relation_type,
                        correlation.hypothesis,
                        json.dumps(list(correlation.tags), default=str),
                        json.dumps(list(correlation.row_ids), default=str),
                        float(correlation.created_ts),
                    ),
                )
                for note_id in note_ids:
                    head = conn.execute(
                        "SELECT r.row_id AS row_id FROM notes n JOIN revisions r "
                        "ON r.note_id = n.note_id AND r.revision_num = n.head_rev "
                        "WHERE n.note_id = ?",
                        (note_id,),
                    ).fetchone()
                    if head is None:
                        raise NoteNotFoundError(f"Note {note_id} does not exist.")
                    conn.execute(
                        "INSERT INTO correlation_notes(correlation_id, note_id, row_id) "
                        "VALUES (?, ?, ?)",
                        (correlation.correlation_id, note_id, head["row_id"]),
                    )
        except sqlite3.IntegrityError as exc:
            raise NotesStoreError(f"Could not save the correlation: {exc}") from exc

    def all_correlations(self) -> list[CorrelationRow]:
        conn = self._db.connection()
        members: dict[str, list[str]] = {}
        for r in conn.execute(
            "SELECT correlation_id, note_id FROM correlation_notes ORDER BY rowid"
        ):
            members.setdefault(r["correlation_id"], []).append(r["note_id"])
        out: list[CorrelationRow] = []
        for r in conn.execute("SELECT * FROM correlations ORDER BY rowid"):
            out.append(
                CorrelationRow(
                    correlation_id=r["correlation_id"],
                    note_ids=members.get(r["correlation_id"], []),
                    row_ids=_loads(r["row_ids"], []),
                    time_window=r["time_window"] or "",
                    relation_type=r["relation_type"] or "",
                    hypothesis=r["hypothesis"] or "",
                    tags=_loads(r["tags"], []),
                    created_ts=float(r["created_ts"] or 0.0),
                )
            )
        return out

    def correlations_for_note_ids(self, note_ids: Iterable[str]) -> list[CorrelationRow]:
        wanted = set(note_ids)
        return [c for c in self.all_correlations() if wanted.intersection(c.note_ids)]


__all__ = [
    "NoteConflictError",
    "NoteNotFoundError",
    "NotesStore",
    "NotesStoreError",
    "OwnerRecord",
    "SCHEMA_VERSION",
    "SubjectRecord",
    "UnknownSubjectError",
]
