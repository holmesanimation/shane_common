"""Immutable runtime-observation models for configured storage.

These models are deliberately separate from `StorageProfile` and
`StorageLocation`. Configuration/routing truth remains stable while the
observations below describe what the filesystem reports *now*.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from .volume import StorageVolume


class AvailabilityStatus(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    READ_ONLY = "READ_ONLY"
    DEGRADED = "DEGRADED"
    UNKNOWN = "UNKNOWN"


class WritabilityStatus(str, Enum):
    WRITABLE = "WRITABLE"
    READ_ONLY = "READ_ONLY"
    UNKNOWN = "UNKNOWN"


class ObservationDiagnosticCode(str, Enum):
    ROOT_NOT_FOUND = "ROOT_NOT_FOUND"
    VOLUME_UNAVAILABLE = "VOLUME_UNAVAILABLE"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    VOLUME_IDENTITY_FAILED = "VOLUME_IDENTITY_FAILED"
    WRITABILITY_UNKNOWN = "WRITABILITY_UNKNOWN"
    CAPACITY_QUERY_FAILED = "CAPACITY_QUERY_FAILED"


def utc_now() -> datetime:
    """Return an aware UTC timestamp.

    Kept as a function so callers/tests can inject a clock into the observer
    without monkey-patching datetime.
    """

    return datetime.now(timezone.utc)


def _require_aware_utc(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    if value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{field_name} must be UTC")


@dataclass(frozen=True, slots=True)
class StorageProfileObservation:
    """Runtime state of one configured filesystem storage profile root."""

    profile_id: str
    root: Path
    availability_status: AvailabilityStatus
    writability_status: WritabilityStatus
    volume_id: str | None
    observed_at_utc: datetime
    diagnostic_code: ObservationDiagnosticCode | None = None
    diagnostic_message: str | None = None

    def __post_init__(self) -> None:
        profile_id = self.profile_id.strip()
        if not profile_id:
            raise ValueError("profile_id must be non-empty")
        _require_aware_utc(self.observed_at_utc, "observed_at_utc")
        object.__setattr__(self, "profile_id", profile_id)
        object.__setattr__(self, "root", Path(self.root))

        if self.volume_id is not None:
            volume_id = self.volume_id.strip()
            object.__setattr__(self, "volume_id", volume_id or None)


@dataclass(frozen=True, slots=True)
class StorageCapacityObservation:
    """Raw capacity facts for one filesystem capacity domain.

    `None` means unknown. Failed capacity queries must never be represented by
    fabricated zeros.
    """

    volume_id: str
    capacity_bytes: int | None
    used_bytes: int | None
    free_bytes: int | None
    observed_at_utc: datetime
    diagnostic_code: ObservationDiagnosticCode | None = None
    diagnostic_message: str | None = None

    def __post_init__(self) -> None:
        volume_id = self.volume_id.strip()
        if not volume_id:
            raise ValueError("volume_id must be non-empty")
        _require_aware_utc(self.observed_at_utc, "observed_at_utc")
        object.__setattr__(self, "volume_id", volume_id)

        values = (self.capacity_bytes, self.used_bytes, self.free_bytes)
        known_count = sum(value is not None for value in values)
        if known_count not in (0, 3):
            raise ValueError(
                "capacity_bytes, used_bytes, and free_bytes must be all known or all unknown"
            )

        if known_count == 3:
            assert self.capacity_bytes is not None
            assert self.used_bytes is not None
            assert self.free_bytes is not None
            if min(self.capacity_bytes, self.used_bytes, self.free_bytes) < 0:
                raise ValueError("capacity values must be >= 0")
            if self.used_bytes > self.capacity_bytes:
                raise ValueError("used_bytes cannot exceed capacity_bytes")
            if self.free_bytes > self.capacity_bytes:
                raise ValueError("free_bytes cannot exceed capacity_bytes")

    @property
    def percent_used(self) -> float | None:
        if self.capacity_bytes is None or self.used_bytes is None:
            return None
        if self.capacity_bytes == 0:
            return 0.0
        return (self.used_bytes / self.capacity_bytes) * 100.0


@dataclass(frozen=True, slots=True)
class StorageObservationSnapshot:
    """One coherent multi-profile observation pass."""

    profile_observations: tuple[StorageProfileObservation, ...]
    volumes: tuple[StorageVolume, ...]
    capacity_observations: tuple[StorageCapacityObservation, ...]
    observed_at_utc: datetime

    def __post_init__(self) -> None:
        _require_aware_utc(self.observed_at_utc, "observed_at_utc")

        profile_ids = [item.profile_id for item in self.profile_observations]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("profile_observations contain duplicate profile_id values")

        volume_ids = [item.volume_id for item in self.volumes]
        if len(volume_ids) != len(set(volume_ids)):
            raise ValueError("volumes contain duplicate volume_id values")

        capacity_ids = [item.volume_id for item in self.capacity_observations]
        if len(capacity_ids) != len(set(capacity_ids)):
            raise ValueError("capacity_observations contain duplicate volume_id values")
