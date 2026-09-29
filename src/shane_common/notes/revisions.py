# shane_common/notes/revisions.py
"""Append a new revision (edit or delete) of an existing note.

Storage is append-only: an edit or delete never rewrites an earlier revision,
it inserts the next ``revision_num`` under the same note ID.  Table-row fields
(``table_id``, ``row_id`` ...) are carried over from the revision being edited.

If the note changed since *row* was read, the store rejects the write with
``NoteConflictError`` rather than interleaving the two edits.

No Qt dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shane_common.notes.model import (
    OP_DELETE,
    OP_EDIT,
    Note,
    NoteRow,
    NoteType,
)
from shane_common.notes.notes_writer import NotesWriter


@dataclass(frozen=True)
class CommittedRevision:
    note: Note
    path: Path
    note_id: str
    revision_num: int
    context: dict


def _note_type(row: NoteRow) -> NoteType:
    try:
        return NoteType(row.note_type) if row.note_type else NoteType.GENERAL
    except ValueError:
        return NoteType.GENERAL


def commit_revision(
    writer: NotesWriter,
    row: NoteRow,
    *,
    op: str,
    text: str,
    title: str | None,
    subject: str,
    context_updates: dict[str, Any] | None = None,
) -> CommittedRevision:
    """Append revision ``row.revision_num + 1`` of *row*'s note.

    *writer* must be bound to the note's owner (``writer.for_owner(...)``).
    Raises ``NotesStoreError`` subclasses when the store rejects the write.
    """
    if op not in (OP_EDIT, OP_DELETE):
        raise ValueError(f"unsupported revision op: {op!r}")
    if not row.note_id:
        raise ValueError("cannot revise a note that has no note_id")

    revision_num = (row.revision_num or 1) + 1
    context = {**(row.context or {}), **(context_updates or {})}

    note = writer.build_note(
        note_id=row.note_id,
        revision_num=revision_num,
        op=op,
        note_type=_note_type(row),
        text=text,
        instrument=row.instrument,
        strategy_id=row.strategy_id,
        context=context,
        title=title,
        subject=subject,
        owner_module=row.owner_module,
        owner_class=row.owner_class,
        table_id=row.table_id,
        table_label=row.table_label,
        row_id=row.row_id,
        row_ts=row.row_ts,
        row_snapshot=row.row_snapshot,
    )
    path = writer.commit(note)
    return CommittedRevision(
        note=note, path=path, note_id=row.note_id, revision_num=revision_num, context=context
    )
