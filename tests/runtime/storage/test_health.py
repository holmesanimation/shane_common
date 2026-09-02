from dataclasses import dataclass
import pytest

from shane_common.runtime.storage.health import (
    CapacityHealthTrigger, CapacityPolicyConfigurationError, CapacityStatus,
    CapacityThresholdPolicy, CapacityThresholds, evaluate_capacity_health,
    load_capacity_threshold_policy,
)

@dataclass(frozen=True)
class Observation:
    capacity_bytes: int | None
    used_bytes: int | None
    free_bytes: int | None
    percent_used: float | None

def policy():
    return CapacityThresholdPolicy(
        warning=CapacityThresholds(percent_used=80, free_bytes=100),
        critical=CapacityThresholds(percent_used=90, free_bytes=25),
    )

@pytest.mark.parametrize("kwargs", [
    {"percent_used": -1, "free_bytes": 1},
    {"percent_used": 101, "free_bytes": 1},
    {"percent_used": float("nan"), "free_bytes": 1},
    {"percent_used": 80, "free_bytes": -1},
    {"percent_used": True, "free_bytes": 1},
    {"percent_used": 80, "free_bytes": True},
])
def test_threshold_value_validation(kwargs):
    with pytest.raises(CapacityPolicyConfigurationError):
        CapacityThresholds(**kwargs)

def test_policy_relationship_validation():
    with pytest.raises(CapacityPolicyConfigurationError):
        CapacityThresholdPolicy(
            warning=CapacityThresholds(80, 100),
            critical=CapacityThresholds(79, 25),
        )
    with pytest.raises(CapacityPolicyConfigurationError):
        CapacityThresholdPolicy(
            warning=CapacityThresholds(80, 100),
            critical=CapacityThresholds(90, 101),
        )

def test_healthy():
    result = evaluate_capacity_health(Observation(1000, 700, 300, 70.0), policy())
    assert result.status is CapacityStatus.HEALTHY
    assert result.triggers == ()

def test_warning_from_percent_only():
    result = evaluate_capacity_health(Observation(1000, 830, 170, 83.0), policy())
    assert result.status is CapacityStatus.WARNING
    assert result.triggers == (CapacityHealthTrigger.PERCENT_USED_WARNING,)

def test_warning_from_free_only():
    result = evaluate_capacity_health(Observation(1000, 750, 75, 75.0), policy())
    assert result.status is CapacityStatus.WARNING
    assert result.triggers == (CapacityHealthTrigger.FREE_BYTES_WARNING,)

def test_critical_from_free_only():
    result = evaluate_capacity_health(Observation(1000, 720, 18, 72.0), policy())
    assert result.status is CapacityStatus.CRITICAL
    assert result.triggers == (CapacityHealthTrigger.FREE_BYTES_CRITICAL,)

def test_most_severe_wins_and_all_triggers_are_preserved():
    result = evaluate_capacity_health(Observation(1000, 920, 80, 92.0), policy())
    assert result.status is CapacityStatus.CRITICAL
    assert result.triggers == (
        CapacityHealthTrigger.PERCENT_USED_CRITICAL,
        CapacityHealthTrigger.FREE_BYTES_WARNING,
    )

def test_exact_boundaries_trigger():
    warning = evaluate_capacity_health(Observation(1000, 800, 100, 80.0), policy())
    critical = evaluate_capacity_health(Observation(1000, 900, 25, 90.0), policy())
    assert warning.status is CapacityStatus.WARNING
    assert critical.status is CapacityStatus.CRITICAL

@pytest.mark.parametrize("observation", [
    Observation(None, 1, 1, 1.0), Observation(1, None, 1, 1.0),
    Observation(1, 1, None, 1.0), Observation(1, 1, 1, None),
    Observation(1, 1, 1, float("nan")),
])
def test_incomplete_or_nonfinite_observation_is_unknown(observation):
    result = evaluate_capacity_health(observation, policy())
    assert result.status is CapacityStatus.UNKNOWN
    assert result.triggers == (CapacityHealthTrigger.OBSERVATION_UNKNOWN,)


def test_load_capacity_threshold_policy_from_yaml(tmp_path):
    policy_path = tmp_path / "health_policy.yaml"
    policy_path.write_text(
        "schema_version: 1\n"
        "warning:\n"
        "  percent_used: 80\n"
        "  free_bytes: 100\n"
        "critical:\n"
        "  percent_used: 90\n"
        "  free_bytes: 25\n",
        encoding="utf-8",
    )
    loaded = load_capacity_threshold_policy(policy_path)
    assert loaded == policy()


def test_load_capacity_threshold_policy_missing_file(tmp_path):
    with pytest.raises(CapacityPolicyConfigurationError):
        load_capacity_threshold_policy(tmp_path / "missing.yaml")


def test_load_capacity_threshold_policy_missing_schema_version(tmp_path):
    policy_path = tmp_path / "health_policy.yaml"
    policy_path.write_text("warning: {percent_used: 80, free_bytes: 100}\ncritical: {percent_used: 90, free_bytes: 25}\n", encoding="utf-8")
    with pytest.raises(CapacityPolicyConfigurationError):
        load_capacity_threshold_policy(policy_path)
