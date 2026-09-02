"""Generic storage-observation categorical transition detection.

Detects meaningful category-level state changes (availability, writability,
capacity health, volume identity) between two monitoring passes, ignoring
ordinary numeric drift.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping

from .health import CapacityStatus


class StorageTransitionKind(str, Enum):
    AVAILABILITY_CHANGED = "AVAILABILITY_CHANGED"
    WRITABILITY_CHANGED = "WRITABILITY_CHANGED"
    CAPACITY_STATUS_CHANGED = "CAPACITY_STATUS_CHANGED"
    VOLUME_ID_CHANGED = "VOLUME_ID_CHANGED"


@dataclass(frozen=True, slots=True)
class StorageProfileHealthState:
    """Normalized categorical monitoring state for one configured profile."""

    profile_id: str
    availability: object
    writability: object
    volume_id: str | None
    capacity_status: CapacityStatus


@dataclass(frozen=True, slots=True)
class StorageStateTransition:
    profile_id: str
    kind: StorageTransitionKind
    previous: object
    current: object
    volume_id: str | None = None


def compare_storage_states(
    previous: Mapping[str, StorageProfileHealthState] | None,
    current: Mapping[str, StorageProfileHealthState],
) -> tuple[StorageStateTransition, ...]:
    """Return categorical changes; an initial baseline emits no transitions."""

    if previous is None:
        return ()

    transitions: list[StorageStateTransition] = []
    for profile_id in sorted(current.keys() & previous.keys()):
        before = previous[profile_id]
        after = current[profile_id]

        if before.availability != after.availability:
            transitions.append(StorageStateTransition(
                profile_id, StorageTransitionKind.AVAILABILITY_CHANGED,
                before.availability, after.availability, after.volume_id or before.volume_id,
            ))

        if before.writability != after.writability:
            transitions.append(StorageStateTransition(
                profile_id, StorageTransitionKind.WRITABILITY_CHANGED,
                before.writability, after.writability, after.volume_id or before.volume_id,
            ))

        if before.capacity_status != after.capacity_status:
            transitions.append(StorageStateTransition(
                profile_id, StorageTransitionKind.CAPACITY_STATUS_CHANGED,
                before.capacity_status, after.capacity_status, after.volume_id or before.volume_id,
            ))

        if before.volume_id != after.volume_id:
            transitions.append(StorageStateTransition(
                profile_id, StorageTransitionKind.VOLUME_ID_CHANGED,
                before.volume_id, after.volume_id, after.volume_id,
            ))

    return tuple(transitions)
