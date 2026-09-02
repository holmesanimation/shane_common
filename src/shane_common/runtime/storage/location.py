"""Generic semantic-storage resolution result."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class StorageLocation:
    """Resolved configured storage location.

    Runtime observations such as availability, writability, capacity, and
    physical volume identity are intentionally excluded from this WP1 model.
    """

    resource_id: str
    profile_id: str
    backend_type: str
    uri: str
    local_path: Path | None
    durable: bool
    cacheable: bool
