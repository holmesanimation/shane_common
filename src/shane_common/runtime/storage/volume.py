"""Filesystem capacity-domain identity.

`StorageVolume` is intentionally a filesystem/capacity-domain model, not a
physical-disk inventory model. Multiple configured storage roots that resolve
to the same capacity domain should reference the same `volume_id`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StorageVolume:
    """Stable identity and mount metadata for one filesystem capacity domain."""

    volume_id: str
    mount_point: Path
    filesystem_type: str | None = None

    def __post_init__(self) -> None:
        volume_id = self.volume_id.strip()
        if not volume_id:
            raise ValueError("volume_id must be a non-empty string")

        mount_point = Path(self.mount_point)
        if not str(mount_point):
            raise ValueError("mount_point must be non-empty")

        filesystem_type = self.filesystem_type
        if filesystem_type is not None:
            filesystem_type = filesystem_type.strip() or None

        object.__setattr__(self, "volume_id", volume_id)
        object.__setattr__(self, "mount_point", mount_point)
        object.__setattr__(self, "filesystem_type", filesystem_type)
