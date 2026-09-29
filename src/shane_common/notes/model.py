# shane_common/notes/model.py
"""Plain data types and helpers shared by the notes store, writer and repository.

* ``Note`` is what callers hand to ``NotesWriter.commit`` — one revision of a note.
* ``NoteRow`` is the read model returned by ``NotesRepository`` / ``NotesStore``.
* ``NoteCorrelation`` / ``CorrelationRow`` link notes that share a relationship.

``subject`` is a subject id from the registry, or ``"UNSET"``.  ``UNSET`` is an
API/display value only: the database stores it as ``NULL``.

``op`` is ``"create"``, ``"edit"`` or ``"delete"``.  Storage is append-only, so
an edit or a delete is a new revision (same ``note_id``, higher
``revision_num``); a delete revision hides the note from readers while every
earlier revision stays in the database.

No Qt dependencies.
"""

from __future__ import annotations

import datetime
import secrets
import time as _time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


# ---------------------------------------------------------------------------
# Note taxonomy
# ---------------------------------------------------------------------------

class NoteType(StrEnum):
    BUG = "BUG"
    QUESTION = "QUESTION"
    IMPROVEMENT = "IMPROVEMENT"
    OBSERVATION = "OBSERVATION"
    ANOMALY = "ANOMALY"
    GENERAL = "GENERAL"

    @property
    def display_label(self) -> str:
        _labels: dict[str, str] = {}
        return _labels.get(self.value, self.value.replace("_", " ").title())


# ---------------------------------------------------------------------------
# Constants and ID helpers
# ---------------------------------------------------------------------------

#: Subject value for notes that have no subject assigned.
UNSET_SUBJECT = "UNSET"

#: ``op`` values for a note revision.
OP_CREATE = "create"
OP_EDIT = "edit"
OP_DELETE = "delete"


def clean_title(title: str | None) -> str | None:
    """Normalise a title for storage: stripped, or ``None`` when blank."""
    if title is None:
        return None
    title = str(title).strip()
    return title or None


def clean_subject(subject: str | None) -> str:
    """Normalise a subject for storage: blank/missing becomes ``UNSET``."""
    subject = str(subject).strip() if subject is not None else ""
    return subject or UNSET_SUBJECT


def make_note_id(ts: float | None = None) -> str:
    """Return a stable time-prefixed note ID.

    Format: ``note_YYYYMMDDTHHMMSSXXXZ_<8hex>``
    where XXX is milliseconds and the suffix is 4 random bytes (hex).
    """
    if ts is None:
        ts = _time.time()
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    prefix = dt.strftime("%Y%m%dT%H%M%S") + f"{dt.microsecond // 1000:03d}Z"
    return f"note_{prefix}_{secrets.token_hex(4)}"


def make_correlation_id() -> str:
    """Return a unique 8-byte hex correlation token."""
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# Write model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Note:
    """One revision of a note, as handed to ``NotesWriter.commit``.

    ``note_id`` is assigned by the writer when ``None`` (a brand-new note).
    ``revision_num`` starts at 1 (``op="create"``) and increments for each
    edit or delete.  The table-row fields are only set for notes attached to a
    row of a table (see ``RowNoteDialog``).
    """

    ts: float
    wall_ts: float
    owner: str
    note_type: NoteType
    text: str
    instrument: str | None
    strategy_id: str | None
    context: dict = field(default_factory=dict, hash=False, compare=False)
    title: str | None = None
    subject: str = UNSET_SUBJECT
    note_id: str | None = None
    revision_num: int = 1
    op: str = OP_CREATE
    # --- optional table-row link ------------------------------------------
    owner_module: str | None = None
    owner_class: str | None = None
    table_id: str | None = None
    table_label: str | None = None
    row_id: str | None = None
    row_ts: float | None = None
    row_snapshot: dict = field(default_factory=dict, hash=False, compare=False)


@dataclass(frozen=True)
class NoteCorrelation:
    """Record linking two or more notes that share a causal or temporal relationship."""

    correlation_id: str            # unique stable ID (hex)
    note_ids: list = field(default_factory=list, hash=False, compare=False)
    row_ids: list = field(default_factory=list, hash=False, compare=False)
    time_window: str = ""         # e.g. "2026-05-15T14:30:00Z/PT5M"
    relation_type: str = ""       # e.g. "cause-effect", "concurrent", "sequence"
    hypothesis: str = ""          # free-text analyst note
    tags: list = field(default_factory=list, hash=False, compare=False)
    created_ts: float = 0.0


# ---------------------------------------------------------------------------
# Read model
# ---------------------------------------------------------------------------

@dataclass
class NoteRow:
    """One stored revision of a note."""

    ts: float | None = None
    wall_ts: float | None = None
    note_type: str | None = None
    text: str = ""
    instrument: str | None = None
    strategy_id: str | None = None
    context: dict = field(default_factory=dict)
    title: str | None = None
    subject: str = UNSET_SUBJECT

    note_id: str | None = None
    revision_num: int | None = None
    op: str | None = None
    owner_key: str | None = None
    owner_module: str | None = None
    owner_class: str | None = None
    table_id: str | None = None
    table_label: str | None = None
    row_id: str | None = None
    row_ts: float | None = None
    row_snapshot: dict = field(default_factory=dict)

    @property
    def is_deleted(self) -> bool:
        """True when this revision is a delete tombstone."""
        return self.op == OP_DELETE


@dataclass
class CorrelationRow:
    """A stored correlation record."""

    correlation_id: str = ""
    note_ids: list = field(default_factory=list)
    row_ids: list = field(default_factory=list)
    time_window: str = ""
    relation_type: str = ""
    hypothesis: str = ""
    tags: list = field(default_factory=list)
    created_ts: float = 0.0


def note_row_from_note(note: Note) -> NoteRow:
    """Wrap a just-committed ``Note`` as a ``NoteRow`` (for history lists / the edit flow)."""
    return NoteRow(
        ts=note.ts,
        wall_ts=note.wall_ts,
        note_type=str(note.note_type),
        text=note.text,
        instrument=note.instrument,
        strategy_id=note.strategy_id,
        context=dict(note.context or {}),
        title=clean_title(note.title),
        subject=clean_subject(note.subject),
        note_id=note.note_id,
        revision_num=note.revision_num,
        op=note.op,
        owner_key=note.owner,
        owner_module=note.owner_module,
        owner_class=note.owner_class,
        table_id=note.table_id,
        table_label=note.table_label,
        row_id=note.row_id,
        row_ts=note.row_ts,
        row_snapshot=dict(note.row_snapshot or {}),
    )


# ---------------------------------------------------------------------------
# Revision helpers
# ---------------------------------------------------------------------------

def is_deleted_row(row: Any) -> bool:
    """True if *row* is a delete tombstone (duck-typed: also accepts row stand-ins)."""
    return getattr(row, "op", None) == OP_DELETE


def latest_revisions(rows: list[NoteRow], *, include_deleted: bool = False) -> list[NoteRow]:
    """Collapse *rows* to the latest revision per note, newest ``ts`` first.

    Notes whose latest revision is a delete tombstone are dropped unless
    *include_deleted* is true.
    """
    best: dict[str, NoteRow] = {}
    for r in rows:
        nid = r.note_id or ""
        cur = best.get(nid)
        if cur is None or (r.revision_num or 1) >= (cur.revision_num or 1):
            best[nid] = r
    result = list(best.values())
    if not include_deleted:
        result = [r for r in result if not is_deleted_row(r)]
    return sorted(result, key=lambda r: getattr(r, "ts", 0) or 0, reverse=True)
