"""Generic capacity-health interpretation primitives.

These types are deliberately trading-domain neutral. They interpret raw
`StorageCapacityObservation` facts against a configured threshold policy;
they do not know about availability, writability, or trading-specific
storage resources.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import math
from typing import Protocol, runtime_checkable

import yaml


SUPPORTED_CAPACITY_HEALTH_POLICY_SCHEMA_VERSIONS = frozenset({1})


class CapacityPolicyConfigurationError(ValueError):
    """Raised when a capacity-threshold policy is missing, malformed, or internally inconsistent."""


class CapacityStatus(str, Enum):
    HEALTHY = "HEALTHY"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"
    UNKNOWN = "UNKNOWN"


class CapacityHealthTrigger(str, Enum):
    PERCENT_USED_WARNING = "PERCENT_USED_WARNING"
    PERCENT_USED_CRITICAL = "PERCENT_USED_CRITICAL"
    FREE_BYTES_WARNING = "FREE_BYTES_WARNING"
    FREE_BYTES_CRITICAL = "FREE_BYTES_CRITICAL"
    OBSERVATION_UNKNOWN = "OBSERVATION_UNKNOWN"


@dataclass(frozen=True, slots=True)
class CapacityThresholds:
    percent_used: float
    free_bytes: int

    def __post_init__(self) -> None:
        if isinstance(self.percent_used, bool) or not isinstance(self.percent_used, (int, float)):
            raise CapacityPolicyConfigurationError("percent_used must be a finite number")
        if not math.isfinite(float(self.percent_used)):
            raise CapacityPolicyConfigurationError("percent_used must be finite")
        if not 0.0 <= float(self.percent_used) <= 100.0:
            raise CapacityPolicyConfigurationError(
                f"percent_used must be between 0 and 100 inclusive; got {self.percent_used!r}"
            )
        if isinstance(self.free_bytes, bool) or not isinstance(self.free_bytes, int):
            raise CapacityPolicyConfigurationError("free_bytes must be an integer number of bytes")
        if self.free_bytes < 0:
            raise CapacityPolicyConfigurationError(f"free_bytes must be >= 0; got {self.free_bytes!r}")


@dataclass(frozen=True, slots=True)
class CapacityThresholdPolicy:
    warning: CapacityThresholds
    critical: CapacityThresholds

    def __post_init__(self) -> None:
        if self.critical.percent_used < self.warning.percent_used:
            raise CapacityPolicyConfigurationError(
                "critical.percent_used must be >= warning.percent_used"
            )
        if self.critical.free_bytes > self.warning.free_bytes:
            raise CapacityPolicyConfigurationError(
                "critical.free_bytes must be <= warning.free_bytes"
            )


@dataclass(frozen=True, slots=True)
class CapacityHealthEvaluation:
    status: CapacityStatus
    triggers: tuple[CapacityHealthTrigger, ...] = ()


@runtime_checkable
class CapacityObservation(Protocol):
    """Structural view required by the generic evaluator.

    The accepted WP2 StorageCapacityObservation satisfies this contract.
    """

    capacity_bytes: int | None
    used_bytes: int | None
    free_bytes: int | None
    percent_used: float | None


def evaluate_capacity_health(
    observation: CapacityObservation,
    policy: CapacityThresholdPolicy,
) -> CapacityHealthEvaluation:
    """Interpret one raw capacity observation using the supplied policy."""

    capacity_bytes = observation.capacity_bytes
    used_bytes = observation.used_bytes
    free_bytes = observation.free_bytes
    percent_used = observation.percent_used

    if (
        capacity_bytes is None
        or used_bytes is None
        or free_bytes is None
        or percent_used is None
    ):
        return CapacityHealthEvaluation(
            CapacityStatus.UNKNOWN,
            (CapacityHealthTrigger.OBSERVATION_UNKNOWN,),
        )

    if not math.isfinite(float(percent_used)):
        return CapacityHealthEvaluation(
            CapacityStatus.UNKNOWN,
            (CapacityHealthTrigger.OBSERVATION_UNKNOWN,),
        )

    triggers: list[CapacityHealthTrigger] = []

    if percent_used >= policy.critical.percent_used:
        triggers.append(CapacityHealthTrigger.PERCENT_USED_CRITICAL)
    elif percent_used >= policy.warning.percent_used:
        triggers.append(CapacityHealthTrigger.PERCENT_USED_WARNING)

    if free_bytes <= policy.critical.free_bytes:
        triggers.append(CapacityHealthTrigger.FREE_BYTES_CRITICAL)
    elif free_bytes <= policy.warning.free_bytes:
        triggers.append(CapacityHealthTrigger.FREE_BYTES_WARNING)

    if (
        CapacityHealthTrigger.PERCENT_USED_CRITICAL in triggers
        or CapacityHealthTrigger.FREE_BYTES_CRITICAL in triggers
    ):
        status = CapacityStatus.CRITICAL
    elif triggers:
        status = CapacityStatus.WARNING
    else:
        status = CapacityStatus.HEALTHY

    return CapacityHealthEvaluation(status=status, triggers=tuple(triggers))


def _thresholds_from_mapping(raw: object, *, field_name: str) -> CapacityThresholds:
    if not isinstance(raw, dict):
        raise CapacityPolicyConfigurationError(f"{field_name} must be a YAML mapping")
    if "percent_used" not in raw:
        raise CapacityPolicyConfigurationError(f"{field_name} is missing percent_used")
    if "free_bytes" not in raw:
        raise CapacityPolicyConfigurationError(f"{field_name} is missing free_bytes")
    return CapacityThresholds(
        percent_used=raw["percent_used"],
        free_bytes=raw["free_bytes"],
    )


def load_capacity_threshold_policy(path: str | Path) -> CapacityThresholdPolicy:
    """Load a capacity-threshold policy from YAML with strict, fail-visible semantics.

    Mirrors `shane_common.runtime.profiles.load_runtime_profile`: configuration
    is authoritative and a missing/malformed file fails explicitly rather than
    silently falling back to defaults.
    """

    policy_path = Path(path)
    if not policy_path.is_file():
        raise CapacityPolicyConfigurationError(
            f"Capacity health policy does not exist or is not a file: {policy_path}"
        )

    try:
        with policy_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise CapacityPolicyConfigurationError(
            f"Malformed YAML in capacity health policy {policy_path}: {exc}"
        ) from exc
    except OSError as exc:
        raise CapacityPolicyConfigurationError(
            f"Unable to read capacity health policy {policy_path}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise CapacityPolicyConfigurationError(
            f"Capacity health policy {policy_path} must contain a YAML mapping at the root"
        )

    if "schema_version" not in raw:
        raise CapacityPolicyConfigurationError(
            f"Capacity health policy {policy_path} is missing schema_version"
        )
    schema_version = raw["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise CapacityPolicyConfigurationError("schema_version must be an integer")
    if schema_version not in SUPPORTED_CAPACITY_HEALTH_POLICY_SCHEMA_VERSIONS:
        raise CapacityPolicyConfigurationError(
            f"Unsupported capacity health policy schema_version {schema_version!r}; "
            f"supported versions are {sorted(SUPPORTED_CAPACITY_HEALTH_POLICY_SCHEMA_VERSIONS)}"
        )

    if "warning" not in raw:
        raise CapacityPolicyConfigurationError(
            f"Capacity health policy {policy_path} is missing warning"
        )
    if "critical" not in raw:
        raise CapacityPolicyConfigurationError(
            f"Capacity health policy {policy_path} is missing critical"
        )

    return CapacityThresholdPolicy(
        warning=_thresholds_from_mapping(raw["warning"], field_name="warning"),
        critical=_thresholds_from_mapping(raw["critical"], field_name="critical"),
    )
