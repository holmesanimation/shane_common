"""Strict, domain-neutral runtime-profile loading primitives."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

import yaml


SUPPORTED_RUNTIME_PROFILE_SCHEMA_VERSIONS = frozenset({1})


class RuntimeConfigurationError(ValueError):
    """Raised when runtime/deployment configuration is missing or invalid."""


@dataclass(frozen=True)
class RuntimeProfile:
    """A strict runtime-profile reference document.

    The shared layer deliberately treats environment values and component-profile
    identifiers as generic deployment configuration. Trading-specific validation
    (for example, allowed execution modes) belongs to ``trading_system``.
    """

    schema_version: int
    runtime_profile_id: str
    environment: Mapping[str, Any]
    storage_profile_set: str | None = None
    service_topology: str | None = None
    observability_profile: str | None = None
    network_profile: str | None = None

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_RUNTIME_PROFILE_SCHEMA_VERSIONS:
            raise RuntimeConfigurationError(
                f"Unsupported runtime profile schema_version {self.schema_version!r}; "
                f"supported versions are "
                f"{sorted(SUPPORTED_RUNTIME_PROFILE_SCHEMA_VERSIONS)}"
            )

        if not isinstance(self.runtime_profile_id, str) or not self.runtime_profile_id.strip():
            raise RuntimeConfigurationError(
                "runtime_profile_id must be a non-empty string"
            )

        if not isinstance(self.environment, Mapping):
            raise RuntimeConfigurationError("environment must be a mapping")

        # Prevent callers from mutating the profile's environment after construction.
        object.__setattr__(
            self,
            "environment",
            MappingProxyType(dict(self.environment)),
        )

        for field_name in (
            "storage_profile_set",
            "service_topology",
            "observability_profile",
            "network_profile",
        ):
            value = getattr(self, field_name)
            if value is not None and (
                not isinstance(value, str) or not value.strip()
            ):
                raise RuntimeConfigurationError(
                    f"{field_name} must be a non-empty string when supplied"
                )


def load_runtime_profile(path: str | Path) -> RuntimeProfile:
    """Load a runtime profile from YAML with strict, fail-visible semantics.

    Unlike user-preference/config stores that may legitimately return defaults
    for a missing or malformed file, runtime/deployment configuration is
    authoritative and therefore fails explicitly.
    """

    profile_path = Path(path)
    if not profile_path.is_file():
        raise RuntimeConfigurationError(
            f"Runtime profile does not exist or is not a file: {profile_path}"
        )

    try:
        with profile_path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise RuntimeConfigurationError(
            f"Malformed YAML in runtime profile {profile_path}: {exc}"
        ) from exc
    except OSError as exc:
        raise RuntimeConfigurationError(
            f"Unable to read runtime profile {profile_path}: {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise RuntimeConfigurationError(
            f"Runtime profile {profile_path} must contain a YAML mapping at the root"
        )

    if "schema_version" not in raw:
        raise RuntimeConfigurationError(
            f"Runtime profile {profile_path} is missing schema_version"
        )
    schema_version = raw["schema_version"]
    if isinstance(schema_version, bool) or not isinstance(schema_version, int):
        raise RuntimeConfigurationError("schema_version must be an integer")

    if "runtime_profile_id" not in raw:
        raise RuntimeConfigurationError(
            f"Runtime profile {profile_path} is missing runtime_profile_id"
        )

    environment = raw.get("environment", {})
    if not isinstance(environment, dict):
        raise RuntimeConfigurationError("environment must be a YAML mapping")

    return RuntimeProfile(
        schema_version=schema_version,
        runtime_profile_id=raw["runtime_profile_id"],
        environment=environment,
        storage_profile_set=raw.get("storage_profile_set"),
        service_topology=raw.get("service_topology"),
        observability_profile=raw.get("observability_profile"),
        network_profile=raw.get("network_profile"),
    )
