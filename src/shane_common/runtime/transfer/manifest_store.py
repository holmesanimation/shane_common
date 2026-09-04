"""Generic durable JSON-record ledger with atomic reload-safe persistence.

Domain-neutral: knows nothing about migration states or trading resource
IDs. Records are opaque JSON-serializable dicts keyed by a caller-supplied
string ID. Suitable for manifests with up to a few thousand records (the
whole file is loaded/saved on each mutation) — not a streaming append-only
log.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from shane_common.io.atomic import write_json_atomic


class DurableRecordLedger:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> dict[str, dict[str, Any]]:
        if not self._path.exists():
            return {}
        text = self._path.read_text(encoding="utf-8")
        if not text.strip():
            return {}
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError(f"Ledger file {self._path} does not contain a JSON object")
        return raw

    def save(self, records: Mapping[str, dict[str, Any]]) -> None:
        write_json_atomic(self._path, dict(records))

    def upsert(self, record_id: str, fields: dict[str, Any]) -> None:
        records = self.load()
        records[record_id] = fields
        self.save(records)

    def get(self, record_id: str) -> dict[str, Any] | None:
        return self.load().get(record_id)

    def delete(self, record_id: str) -> None:
        records = self.load()
        records.pop(record_id, None)
        self.save(records)
