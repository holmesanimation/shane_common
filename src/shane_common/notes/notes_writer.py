# shane_common/notes/notes_writer.py
"""Write facade over :class:`~shane_common.notes.store.NotesStore`.

``NotesWriter`` is bound to one owner and one clock.  ``build_note`` stamps a
:class:`Note` (giving it a real ``note_id``), ``commit`` appends it to the
database as one revision.  Edits and deletes go through
``shane_common.notes.revisions.commit_revision``.

The note taxonomy, ID helpers and the ``Note`` / ``NoteCorrelation`` types live
in ``shane_common.notes.model`` and are re-exported here for existing imports.

No Qt dependencies.  Single-threaded write path (GUI commits only).
"""

from __future__ import annotations

import dataclasses
import time as _time
from pathlib import Path
from typing import Any

from shane_common.notes.model import (
    OP_CREATE,
    OP_DELETE,
    OP_EDIT,
    UNSET_SUBJECT,
    Note,
    NoteCorrelation,
    NoteType,
    clean_subject,
    clean_title,
    make_correlation_id,
    make_note_id,
)
from shane_common.notes.store import NotesStore

__all__ = [
    "OP_CREATE",
    "OP_DELETE",
    "OP_EDIT",
    "UNSET_SUBJECT",
    "Note",
    "NoteCorrelation",
    "NoteType",
    "NotesWriter",
    "clean_subject",
    "clean_title",
    "make_correlation_id",
    "make_note_id",
]


class NotesWriter:
    """Append notes for one owner to a :class:`NotesStore`.

    Parameters
    ----------
    store:
        The notes database.
    owner:
        Owner key the notes are filed under (e.g. ``"journal"``).  Owners the
        registry does not know yet are registered on first write.
    app_clock:
        Object exposing ``now_ts: float``.  May be ``None``, in which case wall
        time is used.
    """

    def __init__(self, store: NotesStore, owner: str, app_clock: Any = None) -> None:
        self._store = store
        self._owner = str(owner)
        self._app_clock = app_clock

    @property
    def store(self) -> NotesStore:
        return self._store

    @property
    def owner(self) -> str:
        return self._owner

    def for_owner(self, owner: str) -> "NotesWriter":
        """Return a writer for *owner* that shares this writer's store and clock."""
        if str(owner) == self._owner:
            return self
        return NotesWriter(self._store, owner, self._app_clock)

    # ------------------------------------------------------------------
    # Notes
    # ------------------------------------------------------------------

    def now(self) -> float:
        """Current note timestamp: the app clock when there is one, else wall time."""
        return float(self._app_clock.now_ts) if self._app_clock is not None else _time.time()

    def build_note(
        self,
        *,
        note_type: NoteType = NoteType.GENERAL,
        text: str,
        instrument: str | None = None,
        strategy_id: str | None = None,
        context: dict | None = None,
        title: str | None = None,
        subject: str = UNSET_SUBJECT,
        note_id: str | None = None,
        revision_num: int = 1,
        op: str = OP_CREATE,
        owner_module: str | None = None,
        owner_class: str | None = None,
        table_id: str | None = None,
        table_label: str | None = None,
        row_id: str | None = None,
        row_ts: float | None = None,
        row_snapshot: dict | None = None,
    ) -> Note:
        """Build a ``Note`` stamped from the clock, with a fresh ``note_id`` unless given."""
        ts = self.now()
        return Note(
            ts=ts,
            wall_ts=_time.time(),
            owner=self._owner,
            note_type=note_type,
            text=text,
            instrument=instrument,
            strategy_id=strategy_id,
            context=dict(context or {}),
            title=title,
            subject=subject,
            note_id=note_id if note_id is not None else make_note_id(ts),
            revision_num=revision_num,
            op=op,
            owner_module=owner_module,
            owner_class=owner_class,
            table_id=table_id,
            table_label=table_label,
            row_id=row_id,
            row_ts=row_ts,
            row_snapshot=dict(row_snapshot or {}),
        )

    def commit(self, note: Note) -> Path:
        """Append *note* as one revision and return the database path.

        Raises ``NotesStoreError`` subclasses (e.g. ``NoteConflictError``) when
        the store rejects the write; nothing is written in that case.
        """
        if note.note_id is None:
            note = dataclasses.replace(note, note_id=make_note_id(note.ts))
        self._store.add_revision(note)
        return self._store.path

    # ------------------------------------------------------------------
    # Correlations
    # ------------------------------------------------------------------

    def commit_correlation(self, correlation: NoteCorrelation) -> Path:
        """Save *correlation* and return the database path."""
        self._store.add_correlation(correlation)
        return self._store.path

    def build_correlation(
        self,
        *,
        note_ids: list[str],
        row_ids: list[str] | None = None,
        relation_type: str = "",
        hypothesis: str = "",
        tags: list[str] | None = None,
        time_window: str = "",
    ) -> NoteCorrelation:
        """Convenience: build a ``NoteCorrelation`` stamped with the current time."""
        return NoteCorrelation(
            correlation_id=make_correlation_id(),
            note_ids=list(note_ids),
            row_ids=list(row_ids or []),
            time_window=time_window,
            relation_type=relation_type,
            hypothesis=hypothesis,
            tags=list(tags or []),
            created_ts=self.now(),
        )
