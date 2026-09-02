from datetime import datetime, timezone

import pytest

from shane_common.runtime.storage.observation import StorageCapacityObservation


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


def test_percent_used_is_computed() -> None:
    observation = StorageCapacityObservation(
        volume_id="volume-a",
        capacity_bytes=1000,
        used_bytes=250,
        free_bytes=750,
        observed_at_utc=NOW,
    )

    assert observation.percent_used == pytest.approx(25.0)


def test_unknown_capacity_is_not_fabricated_as_zero() -> None:
    observation = StorageCapacityObservation(
        volume_id="volume-a",
        capacity_bytes=None,
        used_bytes=None,
        free_bytes=None,
        observed_at_utc=NOW,
    )

    assert observation.percent_used is None


def test_partial_capacity_is_rejected() -> None:
    with pytest.raises(ValueError, match="all known or all unknown"):
        StorageCapacityObservation(
            volume_id="volume-a",
            capacity_bytes=100,
            used_bytes=None,
            free_bytes=50,
            observed_at_utc=NOW,
        )


def test_timestamp_must_be_utc_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        StorageCapacityObservation(
            volume_id="volume-a",
            capacity_bytes=100,
            used_bytes=50,
            free_bytes=50,
            observed_at_utc=datetime(2026, 9, 1, 12, 0),
        )
