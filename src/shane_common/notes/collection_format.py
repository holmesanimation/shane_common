# shane_common/notes/collection_format.py
"""Pure collection-text formatting helpers for the Notes Browser.

These functions are extracted so they can be unit-tested without Qt.

No Qt dependencies.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import Any

from shane_common.notes.notes_repository import NoteRow

# Collection manifest schema version
COLLECTION_SCHEMA = "notes-collection-v1"


# ---------------------------------------------------------------------------
# Contents panel — single-note detail text
# ---------------------------------------------------------------------------

def format_note_contents(note: NoteRow) -> str:
    """Format a single note for display in the Contents panel."""
    lines: list[str] = []
    if note.note_id:
        lines.append(f"note_id     : {note.note_id}")
    if note.revision_id:
        lines.append(f"revision_id : {note.revision_id}")
        lines.append(f"revision_num: {note.revision_num}  op: {note.op}")
    if note.ts:
        try:
            dt = datetime.datetime.utcfromtimestamp(note.ts).replace(
                tzinfo=datetime.timezone.utc
            )
            lines.append(f"ts (UTC)    : {dt.strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append(f"ts (local)  : {dt.astimezone().strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception:
            lines.append(f"ts          : {note.ts}")
    if note.owner_key or note.owner:
        lines.append(f"owner       : {note.owner_key or note.owner}")
    if note.owner_module:
        lines.append(f"module      : {note.owner_module}.{note.owner_class}")
    if note.table_id:
        lines.append(f"table_id    : {note.table_id}")
    if note.row_id:
        lines.append(f"row_id      : {note.row_id}")
    if note.row_ts:
        try:
            dt_row = datetime.datetime.utcfromtimestamp(note.row_ts).replace(
                tzinfo=datetime.timezone.utc
            )
            lines.append(f"row_ts      : {dt_row.strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception:
            lines.append(f"row_ts      : {note.row_ts}")
    if note.instrument:
        lines.append(f"instrument  : {note.instrument}")
    if note.strategy_id:
        lines.append(f"strategy_id : {note.strategy_id}")
    if note.note_type:
        lines.append(f"note_type   : {note.note_type}")
    lines.append("")
    lines.append("--- ROW SNAPSHOT ---")
    if note.row_snapshot:
        for k, v in note.row_snapshot.items():
            lines.append(f"  {k}: {v}")
    else:
        lines.append("  (no snapshot)")
    lines.append("")
    lines.append("--- NOTE TEXT ---")
    lines.append(note.text or "(empty)")
    tagged_verses = note.context.get("tagged_verses", [])
    tags = note.context.get("tags", [])
    if tagged_verses:
        lines.append("")
        lines.append("--- TAGGED VERSES ---")
        lines.append(", ".join(v.get("display", v.get("key", "")) for v in tagged_verses))
    if tags:
        lines.append("")
        lines.append("--- TAGS ---")
        lines.append(", ".join(f"#{t}" for t in tags))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Collection manifest header
# ---------------------------------------------------------------------------

def build_collection_manifest_header(
    notes: list[NoteRow],
    notes_root: str | Path | None = None,
) -> str:
    """Return a fenced JSON manifest header for the collection text."""
    now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    entries: list[dict[str, Any]] = []
    for n in notes:
        entry: dict[str, Any] = {
            "note_id":      n.note_id,
            "owner_key":    n.owner_key or n.owner,
            "owner_module": n.owner_module,
            "owner_class":  n.owner_class,
            "table_id":     n.table_id,
            "row_id":       n.row_id,
        }
        if n.row_ts:
            try:
                dt_row = datetime.datetime.utcfromtimestamp(n.row_ts).replace(
                    tzinfo=datetime.timezone.utc
                )
                dt_local = dt_row.astimezone()
                entry["row_ts_utc"]   = dt_row.strftime("%Y-%m-%dT%H:%M:%SZ")
                entry["row_ts_local"] = dt_local.strftime("%Y-%m-%d %H:%M:%S %z")
            except Exception:
                entry["row_ts_utc"] = str(n.row_ts)
        entries.append(entry)

    manifest: dict[str, Any] = {
        "schema":       COLLECTION_SCHEMA,
        "generated_at": now_utc,
        "entries":      entries,
    }
    if notes_root is not None:
        manifest["notes_root"] = str(notes_root)

    return "```json\n" + json.dumps(manifest, indent=2, default=str) + "\n```"


# ---------------------------------------------------------------------------
# Individual collection entry
# ---------------------------------------------------------------------------

def build_collection_entry(note: NoteRow) -> str:
    """Return a readable Markdown entry for one note in the collection panel."""
    lines: list[str] = []
    owner = note.owner_key or note.owner or "unknown"
    row_id = note.row_id or "?"
    lines.append(f"### Note: {owner} / {row_id}")
    if note.note_id:
        lines.append(f"note_id: {note.note_id}")
    if note.ts:
        try:
            dt = datetime.datetime.utcfromtimestamp(note.ts).replace(
                tzinfo=datetime.timezone.utc
            ).astimezone()
            lines.append(f"Time: {dt.strftime('%Y-%m-%d %H:%M:%S')}")
        except Exception:
            lines.append(f"Time: {note.ts}")
    if note.row_snapshot:
        lines.append("Row snapshot:")
        for k, v in note.row_snapshot.items():
            lines.append(f"  {k}: {v}")
    lines.append("")
    lines.append(note.text or "(empty)")
    return "\n".join(lines)
