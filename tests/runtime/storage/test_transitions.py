from shane_common.runtime.storage.health import CapacityStatus
from shane_common.runtime.storage.transitions import (
    StorageProfileHealthState, StorageTransitionKind, compare_storage_states,
)

def state(*, availability="AVAILABLE", writability="WRITABLE", volume_id="vol-1", capacity_status=CapacityStatus.HEALTHY):
    return StorageProfileHealthState("local_data", availability, writability, volume_id, capacity_status)

def test_initial_baseline_emits_no_transitions():
    assert compare_storage_states(None, {"local_data": state()}) == ()

def test_unchanged_state_emits_no_transitions():
    current = {"local_data": state()}
    assert compare_storage_states(current, current) == ()

def test_detects_availability_transition():
    result = compare_storage_states(
        {"local_data": state(availability="AVAILABLE")},
        {"local_data": state(availability="UNAVAILABLE")},
    )
    assert [x.kind for x in result] == [StorageTransitionKind.AVAILABILITY_CHANGED]

def test_detects_writability_transition():
    result = compare_storage_states(
        {"local_data": state(writability="WRITABLE")},
        {"local_data": state(writability="READ_ONLY")},
    )
    assert [x.kind for x in result] == [StorageTransitionKind.WRITABILITY_CHANGED]

def test_detects_capacity_status_transition():
    result = compare_storage_states(
        {"local_data": state(capacity_status=CapacityStatus.HEALTHY)},
        {"local_data": state(capacity_status=CapacityStatus.WARNING)},
    )
    assert [x.kind for x in result] == [StorageTransitionKind.CAPACITY_STATUS_CHANGED]

def test_detects_volume_identity_transition():
    result = compare_storage_states(
        {"local_data": state(volume_id="vol-1")},
        {"local_data": state(volume_id="vol-2")},
    )
    assert [x.kind for x in result] == [StorageTransitionKind.VOLUME_ID_CHANGED]
    assert result[0].previous == "vol-1"
    assert result[0].current == "vol-2"

def test_profile_addition_is_not_a_wp3_transition():
    previous = {"local_data": state()}
    current = dict(previous)
    current["archive"] = StorageProfileHealthState(
        "archive", "AVAILABLE", "WRITABLE", "vol-2", CapacityStatus.HEALTHY
    )
    assert compare_storage_states(previous, current) == ()
