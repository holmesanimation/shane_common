# shane_common/notes/notes_writer.py
"""Reusable note taxonomy and writer.

Notes are appended to::

    <notes_root>/<platform>/<run_id>/<owner>.jsonl

Correlations are appended to::

    <notes_root>/<platform>/<run_id>/note_correlations.jsonl

The path is fixed for a given session (no day-bucketing).  ``platform`` is
the running app class name (e.g. ``PurityApp``); ``run_id`` is ``app.run_id``.

No Qt dependencies.  Single-threaded write path (GUI commits only).

Schema versions
---------------
v1 (legacy):
    ts, wall_ts, owner, note_type, text, instrument, strategy_id, context
v2 (table rows):
    schema_version=2 plus: note_id, revision_id, revision_num, op,
    owner_key, owner_label, owner_module, owner_class,
    table_id, table_label, row_id, row_ts, row_snapshot
    (also carries the v1 text/instrument/strategy_id/context fields)
correlation:
    correlation_id, note_ids, row_ids, time_window, relation_type,
    hypothesis, tags, created_ts
"""

from __future__ import annotations

import datetime
import json
import secrets
import time as _time
from dataclasses import dataclass, field, asdict
from enum import StrEnum
from pathlib import Path
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
# ID helpers
# ---------------------------------------------------------------------------

def make_note_id(ts: float | None = None) -> str:
    """Return a stable time-prefixed note ID.

    Format: ``note_YYYYMMDDTHHMMSSXXXZ_<8hex>``
    where XXX is milliseconds and the suffix is 4 random bytes (hex).
    """
    if ts is None:
        ts = _time.time()
    dt = datetime.datetime.utcfromtimestamp(ts)
    prefix = dt.strftime("%Y%m%dT%H%M%S") + f"{dt.microsecond // 1000:03d}Z"
    return f"note_{prefix}_{secrets.token_hex(4)}"


def make_revision_id() -> str:
    """Return a unique 8-byte hex revision token."""
    return secrets.token_hex(8)


def make_correlation_id() -> str:
    """Return a unique 8-byte hex correlation token."""
    return secrets.token_hex(8)


# ---------------------------------------------------------------------------
# Correlation record
# ---------------------------------------------------------------------------

CORRELATIONS_FILENAME = "note_correlations.jsonl"


@dataclass(frozen=True)
class NoteCorrelation:
    """Append-only record linking two or more notes that share a causal or
    temporal relationship.

    Correlations live in a separate JSONL file
    (``note_correlations.jsonl``) in the same run directory as the notes
    they reference.  All fields are stored verbatim — there is no in-place
    mutation.
    """

    correlation_id: str            # unique stable ID (hex)
    note_ids: list = field(default_factory=list, hash=False, compare=False)
    row_ids: list = field(default_factory=list, hash=False, compare=False)
    time_window: str = ""         # e.g. "2026-05-15T14:30:00Z/PT5M"
    relation_type: str = ""       # e.g. "cause-effect", "concurrent", "sequence"
    hypothesis: str = ""          # free-text analyst note
    tags: list = field(default_factory=list, hash=False, compare=False)
    created_ts: float = 0.0


# ---------------------------------------------------------------------------
# Note record (v1 — legacy / Log Viewer)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Note:
    ts: float
    wall_ts: float
    owner: str
    note_type: NoteType
    text: str
    instrument: str | None
    strategy_id: str | None
    context: dict = field(default_factory=dict, hash=False, compare=False)


# ---------------------------------------------------------------------------
# Note record (v2 — table rows)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TableNote:
    """Append-only v2 note for a table row.

    ``note_id`` is stable across revisions.  ``revision_id`` is unique per
    append.  ``revision_num`` starts at 1 (``op="create"``) and increments
    for each edit (``op="edit"``).
    """

    # --- identity ----------------------------------------------------------
    note_id: str
    revision_id: str
    revision_num: int
    op: str                       # "create" or "edit"
    # --- clock -------------------------------------------------------------
    ts: float
    wall_ts: float
    # --- owner metadata ----------------------------------------------------
    owner_key: str
    owner_label: str
    owner_module: str
    owner_class: str
    # --- table -------------------------------------------------------------
    table_id: str
    table_label: str
    # --- row ---------------------------------------------------------------
    row_id: str
    row_ts: float | None
    row_snapshot: dict = field(default_factory=dict, hash=False, compare=False)
    # --- note content ------------------------------------------------------
    note_type: NoteType = NoteType.GENERAL
    text: str = ""
    instrument: str | None = None
    strategy_id: str | None = None
    context: dict = field(default_factory=dict, hash=False, compare=False)


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------

class NotesWriter:
    """Append notes to ``<notes_root>/<platform>/<run_id>/<owner>.jsonl``.

    Parameters
    ----------
    notes_root:
        Root directory for notes storage (string or Path-like).
    owner:
        Logical owner label used as the file stem (e.g. ``"journal"``,
        ``"review"``).  Must be a safe filename component.
    app_clock:
        Object exposing ``now_ts: float``.  May be ``None`` if the caller
        always supplies ``ts`` manually via ``Note(...)``.
    platform:
        App class name (e.g. ``"PurityApp"``).  Used as the second path segment.
    run_id:
        Session run ID.  Used as the third path segment.
    """

    def __init__(
        self,
        notes_root: str,
        owner: str,
        app_clock: Any = None,
        *,
        platform: str = "",
        run_id: str = "",
    ) -> None:
        self._root = Path(str(notes_root))
        self._owner = str(owner)
        self._app_clock = app_clock
        self._platform = str(platform)
        self._run_id = str(run_id)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def commit(self, note: Note) -> Path:
        """Append *note* to the JSONL file and return the resolved path."""
        path = self._root / self._platform / self._run_id / f"{self._owner}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "ts": float(note.ts),
            "wall_ts": float(note.wall_ts),
            "owner": str(note.owner),
            "note_type": str(note.note_type),
            "text": str(note.text),
            "instrument": note.instrument,
            "strategy_id": note.strategy_id,
            "context": dict(note.context or {}),
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        return path

    def commit_v2(self, note: TableNote) -> Path:
        """Append a v2 table note and return the resolved path."""
        path = self._root / self._platform / self._run_id / f"{self._owner}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "schema_version": 2,
            "note_id": str(note.note_id),
            "revision_id": str(note.revision_id),
            "revision_num": int(note.revision_num),
            "op": str(note.op),
            "ts": float(note.ts),
            "wall_ts": float(note.wall_ts),
            "owner_key": str(note.owner_key),
            "owner_label": str(note.owner_label),
            "owner_module": str(note.owner_module),
            "owner_class": str(note.owner_class),
            "table_id": str(note.table_id),
            "table_label": str(note.table_label),
            "row_id": str(note.row_id),
            "row_ts": note.row_ts,
            "row_snapshot": dict(note.row_snapshot or {}),
            "note_type": str(note.note_type),
            "text": str(note.text),
            "instrument": note.instrument,
            "strategy_id": note.strategy_id,
            "context": dict(note.context or {}),
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        return path

    def build_note(
        self,
        *,
        note_type: NoteType,
        text: str,
        instrument: str | None = None,
        strategy_id: str | None = None,
        context: dict | None = None,
    ) -> Note:
        """Convenience: build a ``Note`` stamped from ``app_clock.now_ts``."""
        ts = float(self._app_clock.now_ts) if self._app_clock is not None else _time.time()
        return Note(
            ts=ts,
            wall_ts=_time.time(),
            owner=self._owner,
            note_type=note_type,
            text=text,
            instrument=instrument,
            strategy_id=strategy_id,
            context=dict(context or {}),
        )

    def build_table_note(
        self,
        *,
        note_id: str | None = None,
        revision_num: int = 1,
        op: str = "create",
        owner_key: str,
        owner_label: str,
        owner_module: str,
        owner_class: str,
        table_id: str,
        table_label: str,
        row_id: str,
        row_ts: float | None = None,
        row_snapshot: dict | None = None,
        note_type: NoteType = NoteType.GENERAL,
        text: str = "",
        instrument: str | None = None,
        strategy_id: str | None = None,
        context: dict | None = None,
    ) -> TableNote:
        """Convenience: build a v2 ``TableNote`` stamped from ``app_clock``."""
        ts = float(self._app_clock.now_ts) if self._app_clock is not None else _time.time()
        actual_note_id = note_id if note_id is not None else make_note_id(ts)
        return TableNote(
            note_id=actual_note_id,
            revision_id=make_revision_id(),
            revision_num=revision_num,
            op=op,
            ts=ts,
            wall_ts=_time.time(),
            owner_key=owner_key,
            owner_label=owner_label,
            owner_module=owner_module,
            owner_class=owner_class,
            table_id=table_id,
            table_label=table_label,
            row_id=row_id,
            row_ts=row_ts,
            row_snapshot=dict(row_snapshot or {}),
            note_type=note_type,
            text=text,
            instrument=instrument,
            strategy_id=strategy_id,
            context=dict(context or {}),
        )

    def commit_correlation(self, correlation: NoteCorrelation) -> Path:
        """Append *correlation* to ``note_correlations.jsonl`` in the run directory.

        Returns the resolved path to the correlations file.
        """
        path = self._root / self._platform / self._run_id / CORRELATIONS_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "correlation_id": str(correlation.correlation_id),
            "note_ids":       list(correlation.note_ids),
            "row_ids":        list(correlation.row_ids),
            "time_window":    str(correlation.time_window),
            "relation_type":  str(correlation.relation_type),
            "hypothesis":     str(correlation.hypothesis),
            "tags":           list(correlation.tags),
            "created_ts":     float(correlation.created_ts),
        }
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, default=str) + "\n")
        return path

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
        ts = float(self._app_clock.now_ts) if self._app_clock is not None else _time.time()
        return NoteCorrelation(
            correlation_id=make_correlation_id(),
            note_ids=list(note_ids),
            row_ids=list(row_ids or []),
            time_window=time_window,
            relation_type=relation_type,
            hypothesis=hypothesis,
            tags=list(tags or []),
            created_ts=ts,
        )
