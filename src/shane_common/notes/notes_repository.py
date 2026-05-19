# shane_common/notes/notes_repository.py
"""Read-only index layer for notes stored under notes_root.

Discovers JSONL files, parses them safely, resolves latest revisions by
``note_id``, and provides lookups by ``row_id``, owner, and note ID.
Also discovers and returns correlation records from ``note_correlations.jsonl``.

No Qt dependencies.  No write operations — use ``NotesWriter`` for writes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Parsed row
# ---------------------------------------------------------------------------

@dataclass
class NoteRow:
    """A single parsed line from a notes JSONL file.

    Covers both v1 (legacy) rows and v2 (table) rows.
    Fields absent in v1 rows are ``None`` or empty dict.
    """

    schema_version: int
    raw: dict = field(repr=False)

    # shared fields
    ts: float | None = None
    wall_ts: float | None = None
    note_type: str | None = None
    text: str = ""
    instrument: str | None = None
    strategy_id: str | None = None
    context: dict = field(default_factory=dict)

    # v1-only
    owner: str | None = None

    # v2-only
    note_id: str | None = None
    revision_id: str | None = None
    revision_num: int | None = None
    op: str | None = None
    owner_key: str | None = None
    owner_label: str | None = None
    owner_module: str | None = None
    owner_class: str | None = None
    table_id: str | None = None
    table_label: str | None = None
    row_id: str | None = None
    row_ts: float | None = None
    row_snapshot: dict = field(default_factory=dict)

    @property
    def is_v2(self) -> bool:
        return self.schema_version >= 2

    @property
    def is_legacy(self) -> bool:
        return self.schema_version < 2

    @property
    def effective_owner_key(self) -> str | None:
        """Return owner_key for v2 rows, owner for v1 rows."""
        if self.is_v2:
            return self.owner_key
        return self.owner


# ---------------------------------------------------------------------------
# Internal parsing
# ---------------------------------------------------------------------------

def _parse_row(raw: dict) -> NoteRow:
    sv = int(raw.get("schema_version", 1))
    row = NoteRow(schema_version=sv, raw=raw)

    raw_ts = raw.get("ts")
    row.ts = float(raw_ts) if raw_ts is not None else None
    raw_wall = raw.get("wall_ts")
    row.wall_ts = float(raw_wall) if raw_wall is not None else None
    row.note_type = raw.get("note_type")
    row.text = raw.get("text") or ""
    row.instrument = raw.get("instrument")
    row.strategy_id = raw.get("strategy_id")
    row.context = raw.get("context") or {}

    if sv >= 2:
        row.note_id = raw.get("note_id")
        row.revision_id = raw.get("revision_id")
        raw_revnum = raw.get("revision_num")
        row.revision_num = int(raw_revnum) if raw_revnum is not None else None
        row.op = raw.get("op")
        row.owner_key = raw.get("owner_key")
        row.owner_label = raw.get("owner_label")
        row.owner_module = raw.get("owner_module")
        row.owner_class = raw.get("owner_class")
        row.table_id = raw.get("table_id")
        row.table_label = raw.get("table_label")
        row.row_id = raw.get("row_id")
        raw_row_ts = raw.get("row_ts")
        row.row_ts = float(raw_row_ts) if raw_row_ts is not None else None
        row.row_snapshot = raw.get("row_snapshot") or {}
        row.owner = raw.get("owner_key")
    else:
        row.owner = raw.get("owner")

    return row


def _parse_jsonl(path: Path) -> list[NoteRow]:
    """Parse all valid lines from a JSONL file.  Skips blank or malformed lines."""
    rows: list[NoteRow] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            rows.append(_parse_row(raw))
    return rows


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------

class NotesRepository:
    """Discovers and indexes notes files under ``notes_root``.

    All methods re-read from disk on each call (no in-memory caching), which
    is appropriate for the single-session write volume.

    Parameters
    ----------
    notes_root:
        Root directory for notes storage.
    """

    def __init__(self, notes_root: str | Path) -> None:
        self._root = Path(str(notes_root))

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_jsonl_files(self) -> list[Path]:
        """Return all ``.jsonl`` files under ``notes_root``, sorted by path."""
        if not self._root.exists():
            return []
        return sorted(self._root.rglob("*.jsonl"))

    def list_owners(self) -> list[str]:
        """Return distinct owner keys present on disk, sorted."""
        owners: set[str] = set()
        for path in self.discover_jsonl_files():
            for row in _parse_jsonl(path):
                key = row.effective_owner_key
                if key:
                    owners.add(key)
        return sorted(owners)

    # ------------------------------------------------------------------
    # Bulk access
    # ------------------------------------------------------------------

    def all_rows(self) -> list[NoteRow]:
        """Return every parsed row from every discovered JSONL file."""
        rows: list[NoteRow] = []
        for path in self.discover_jsonl_files():
            rows.extend(_parse_jsonl(path))
        return rows

    def rows_for_owner(self, owner_key: str) -> list[NoteRow]:
        """Return every row (all revisions) whose effective owner key matches."""
        return [
            r for r in self.all_rows()
            if r.effective_owner_key == owner_key
        ]

    # ------------------------------------------------------------------
    # Row-level lookups
    # ------------------------------------------------------------------

    def rows_for_row_id(self, row_id: str) -> list[NoteRow]:
        """Return every note revision attached to a specific table ``row_id``."""
        return [r for r in self.all_rows() if r.row_id == row_id]

    def latest_revision(self, note_id: str) -> NoteRow | None:
        """Return the row with the highest ``revision_num`` for ``note_id``."""
        candidates = [
            r for r in self.all_rows()
            if r.note_id is not None and r.note_id == note_id
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda r: r.revision_num or 0)

    def latest_notes_for_row(self, row_id: str) -> list[NoteRow]:
        """Return the latest revision of each distinct note for ``row_id``.

        Groups all revisions by ``note_id``, then selects the highest
        ``revision_num`` per group.  Result is sorted by ``ts`` ascending.
        Legacy rows without a ``note_id`` are each treated as their own note.
        """
        all_revisions = self.rows_for_row_id(row_id)
        by_note: dict[str, list[NoteRow]] = {}
        for r in all_revisions:
            key = r.note_id or r.revision_id or str(id(r))
            by_note.setdefault(key, []).append(r)

        result: list[NoteRow] = []
        for revisions in by_note.values():
            best = max(revisions, key=lambda r: r.revision_num or 0)
            result.append(best)
        return sorted(result, key=lambda r: r.ts or 0.0)

    def row_id_to_note_count(self, owner_key: str) -> dict[str, int]:
        """Return ``{row_id: count}`` of distinct latest-revision notes per row."""
        owner_rows = self.rows_for_owner(owner_key)

        by_note: dict[str, NoteRow] = {}
        legacy_rows: list[NoteRow] = []
        for r in owner_rows:
            if r.note_id:
                existing = by_note.get(r.note_id)
                if existing is None or (r.revision_num or 0) > (existing.revision_num or 0):
                    by_note[r.note_id] = r
            elif r.row_id:
                legacy_rows.append(r)

        by_row: dict[str, set[str]] = {}
        for note_row in by_note.values():
            if note_row.row_id:
                by_row.setdefault(note_row.row_id, set()).add(note_row.note_id)  # type: ignore[arg-type]
        for legacy in legacy_rows:
            if legacy.row_id:
                by_row.setdefault(legacy.row_id, set()).add(str(id(legacy)))

        return {rid: len(note_ids) for rid, note_ids in by_row.items()}

    # ------------------------------------------------------------------
    # Correlation access
    # ------------------------------------------------------------------

    def discover_correlation_files(self) -> list[Path]:
        """Return all ``note_correlations.jsonl`` files under ``notes_root``."""
        if not self._root.exists():
            return []
        return sorted(self._root.rglob(_CORRELATIONS_FILENAME))

    def all_correlations(self) -> list["CorrelationRow"]:
        """Return every parsed correlation record from every discovered file."""
        rows: list[CorrelationRow] = []
        for path in self.discover_correlation_files():
            rows.extend(_parse_correlations_jsonl(path))
        return rows

    def correlations_for_note_id(self, note_id: str) -> list["CorrelationRow"]:
        """Return correlations that reference *note_id*."""
        return [c for c in self.all_correlations() if note_id in c.note_ids]

    def correlations_for_note_ids(
        self, note_ids: list[str]
    ) -> list["CorrelationRow"]:
        """Return correlations that reference at least one of *note_ids*."""
        id_set = set(note_ids)
        return [c for c in self.all_correlations() if id_set.intersection(c.note_ids)]


# ---------------------------------------------------------------------------
# Correlation record
# ---------------------------------------------------------------------------

_CORRELATIONS_FILENAME = "note_correlations.jsonl"


@dataclass
class CorrelationRow:
    """A single parsed line from ``note_correlations.jsonl``."""

    raw: dict = field(repr=False)
    correlation_id: str = ""
    note_ids: list = field(default_factory=list)
    row_ids: list = field(default_factory=list)
    time_window: str = ""
    relation_type: str = ""
    hypothesis: str = ""
    tags: list = field(default_factory=list)
    created_ts: float = 0.0


def _parse_correlation_row(raw: dict) -> CorrelationRow:
    row = CorrelationRow(raw=raw)
    row.correlation_id = str(raw.get("correlation_id") or "")
    row.note_ids       = list(raw.get("note_ids") or [])
    row.row_ids        = list(raw.get("row_ids") or [])
    row.time_window    = str(raw.get("time_window") or "")
    row.relation_type  = str(raw.get("relation_type") or "")
    row.hypothesis     = str(raw.get("hypothesis") or "")
    row.tags           = list(raw.get("tags") or [])
    raw_ts = raw.get("created_ts")
    row.created_ts = float(raw_ts) if raw_ts is not None else 0.0
    return row


def _parse_correlations_jsonl(path: Path) -> list[CorrelationRow]:
    """Parse all valid lines from a correlations JSONL file."""
    rows: list[CorrelationRow] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return rows
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            rows.append(_parse_correlation_row(raw))
    return rows
