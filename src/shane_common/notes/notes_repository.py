# shane_common/notes/notes_repository.py
"""Read facade over :class:`~shane_common.notes.store.NotesStore`.

Keeps the method names the UI already uses (``rows_for_owner``,
``latest_notes_for_owner``, ``latest_notes_for_row`` ...) but every call is a
query against the notes database — there are no files to discover or parse.
No write operations: use ``NotesWriter`` for writes.

``NoteRow``, ``CorrelationRow`` and ``latest_revisions`` are re-exported from
``shane_common.notes.model``.

No Qt dependencies.
"""

from __future__ import annotations

from pathlib import Path

from shane_common.notes.model import (
    CorrelationRow,
    NoteRow,
    is_deleted_row,
    latest_revisions,
)
from shane_common.notes.store import NotesStore

__all__ = [
    "CorrelationRow",
    "NoteRow",
    "NotesRepository",
    "is_deleted_row",
    "latest_revisions",
]


class NotesRepository:
    """Query notes and correlations from a :class:`NotesStore`."""

    def __init__(self, store: NotesStore) -> None:
        self._store = store

    @property
    def store(self) -> NotesStore:
        return self._store

    @property
    def location(self) -> Path:
        """Folder holding the notes database (for "open in Explorer")."""
        return self._store.path.parent

    # -- owners --------------------------------------------------------------

    def list_owners(self) -> list[str]:
        """Return distinct owner keys that have notes, sorted."""
        return self._store.list_owner_keys()

    # -- bulk access ---------------------------------------------------------

    def all_rows(self) -> list[NoteRow]:
        """Return every stored revision of every note."""
        return self._store.all_rows()

    def rows_for_owner(self, owner_key: str) -> list[NoteRow]:
        """Return every revision of every note owned by *owner_key*."""
        return self._store.rows_for_owner(owner_key)

    def latest_notes_for_owner(
        self, owner_key: str, *, include_deleted: bool = False, newest_first: bool = False
    ) -> list[NoteRow]:
        """Return the latest revision of each note for *owner_key*, oldest first.

        Notes whose latest revision is a delete tombstone are omitted unless
        *include_deleted* is true.
        """
        return self._store.latest_notes_for_owner(
            owner_key, include_deleted=include_deleted, newest_first=newest_first
        )

    # -- row-level lookups ---------------------------------------------------

    def rows_for_row_id(self, row_id: str) -> list[NoteRow]:
        """Return every note revision attached to a specific table ``row_id``."""
        return self._store.rows_for_row_id(row_id)

    def latest_revision(self, note_id: str) -> NoteRow | None:
        """Return the row with the highest ``revision_num`` for ``note_id``."""
        return self._store.latest_revision(note_id)

    def note_history(self, note_id: str) -> list[NoteRow]:
        """Return every revision of one note, oldest first."""
        return self._store.note_history(note_id)

    def latest_notes_for_row(self, row_id: str) -> list[NoteRow]:
        """Return the latest revision of each live note for ``row_id``, oldest first."""
        return self._store.latest_notes_for_row(row_id)

    def row_id_to_note_count(self, owner_key: str) -> dict[str, int]:
        """Return ``{row_id: count}`` of live notes per row."""
        return self._store.row_id_to_note_count(owner_key)

    # -- correlations --------------------------------------------------------

    def all_correlations(self) -> list[CorrelationRow]:
        """Return every stored correlation."""
        return self._store.all_correlations()

    def correlations_for_note_id(self, note_id: str) -> list[CorrelationRow]:
        """Return correlations that reference *note_id*."""
        return self._store.correlations_for_note_ids([note_id])

    def correlations_for_note_ids(self, note_ids: list[str]) -> list[CorrelationRow]:
        """Return correlations that reference at least one of *note_ids*."""
        return self._store.correlations_for_note_ids(note_ids)
