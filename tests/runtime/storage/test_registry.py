"""Tests for generic semantic StorageRegistry behavior."""

from pathlib import Path

import pytest

from shane_common.runtime.storage.profile import (
    StorageConfigurationError,
    StorageProfile,
)
from shane_common.runtime.storage.registry import (
    StorageRegistry,
    StorageResourceRegistration,
    StorageTemplateError,
    UnknownStorageProfileError,
    UnknownStorageResourceError,
)


def _profiles(tmp_path: Path) -> tuple[StorageProfile, ...]:
    return (
        StorageProfile(
            profile_id="system",
            backend_type="filesystem",
            root=tmp_path / "system",
        ),
        StorageProfile(
            profile_id="local_data",
            backend_type="filesystem",
            root=tmp_path / "data",
        ),
    )


def test_duplicate_profile_rejected(tmp_path: Path) -> None:
    profile = StorageProfile(
        profile_id="system",
        backend_type="filesystem",
        root=tmp_path / "system",
    )
    with pytest.raises(StorageConfigurationError, match="Duplicate"):
        StorageRegistry(profiles=(profile, profile), resources=())


def test_duplicate_resource_rejected(tmp_path: Path) -> None:
    resource = StorageResourceRegistration(
        resource_id="logs.system",
        profile_id="system",
        path_template="logs",
    )
    with pytest.raises(StorageConfigurationError, match="Duplicate"):
        StorageRegistry(
            profiles=_profiles(tmp_path),
            resources=(resource, resource),
        )


def test_unknown_profile_reference_rejected(tmp_path: Path) -> None:
    resource = StorageResourceRegistration(
        resource_id="logs.system",
        profile_id="missing",
        path_template="logs",
    )
    with pytest.raises(UnknownStorageProfileError, match="unknown profile"):
        StorageRegistry(profiles=_profiles(tmp_path), resources=(resource,))


def test_unknown_resource_fails(tmp_path: Path) -> None:
    registry = StorageRegistry(profiles=_profiles(tmp_path), resources=())

    with pytest.raises(UnknownStorageResourceError, match="Unknown storage resource"):
        registry.resolve("missing")


def test_resolve_returns_location_and_path(tmp_path: Path) -> None:
    resource = StorageResourceRegistration(
        resource_id="marketdata.bars",
        profile_id="local_data",
        path_template="marketdata/bars/{provider}/{instrument}/{timeframe}",
    )
    registry = StorageRegistry(
        profiles=_profiles(tmp_path),
        resources=(resource,),
    )

    location = registry.resolve(
        "marketdata.bars",
        provider="ibkr",
        instrument="NVDA",
        timeframe="1m",
    )

    expected = tmp_path / "data" / "marketdata" / "bars" / "ibkr" / "NVDA" / "1m"
    assert location.local_path == expected
    assert location.resource_id == "marketdata.bars"
    assert location.profile_id == "local_data"
    assert location.backend_type == "filesystem"
    assert location.uri.startswith("file:")
    assert registry.resolve_path(
        "marketdata.bars",
        provider="ibkr",
        instrument="NVDA",
        timeframe="1m",
    ) == expected


def test_missing_template_parameter_fails(tmp_path: Path) -> None:
    resource = StorageResourceRegistration(
        resource_id="marketdata.bars",
        profile_id="local_data",
        path_template="marketdata/{instrument}/{timeframe}",
    )
    registry = StorageRegistry(
        profiles=_profiles(tmp_path),
        resources=(resource,),
    )

    with pytest.raises(StorageTemplateError, match="Missing"):
        registry.resolve("marketdata.bars", instrument="NVDA")


def test_extra_template_parameter_fails(tmp_path: Path) -> None:
    resource = StorageResourceRegistration(
        resource_id="logs.system",
        profile_id="system",
        path_template="logs",
    )
    registry = StorageRegistry(
        profiles=_profiles(tmp_path),
        resources=(resource,),
    )

    with pytest.raises(StorageTemplateError, match="Unexpected"):
        registry.resolve("logs.system", extra="x")


@pytest.mark.parametrize(
    "value",
    ["../Windows", "..", "/absolute", r"C:\Windows", r"nested\part", "nested/part"],
)
def test_template_parameter_cannot_inject_paths(tmp_path: Path, value: str) -> None:
    resource = StorageResourceRegistration(
        resource_id="marketdata.bars",
        profile_id="local_data",
        path_template="marketdata/{instrument}",
    )
    registry = StorageRegistry(
        profiles=_profiles(tmp_path),
        resources=(resource,),
    )

    with pytest.raises(StorageTemplateError):
        registry.resolve("marketdata.bars", instrument=value)


def test_static_parent_traversal_template_rejected() -> None:
    with pytest.raises(StorageConfigurationError, match="parent traversal"):
        StorageResourceRegistration(
            resource_id="logs.system",
            profile_id="system",
            path_template="../logs",
        )


def test_absolute_template_rejected() -> None:
    with pytest.raises(StorageConfigurationError, match="relative"):
        StorageResourceRegistration(
            resource_id="logs.system",
            profile_id="system",
            path_template="/logs",
        )


def test_resolution_does_not_create_directories(tmp_path: Path) -> None:
    data_root = tmp_path / "data"
    target = data_root / "logs"
    resource = StorageResourceRegistration(
        resource_id="logs.system",
        profile_id="local_data",
        path_template="logs",
    )
    registry = StorageRegistry(
        profiles=_profiles(tmp_path),
        resources=(resource,),
    )

    resolved = registry.resolve_path("logs.system")

    assert resolved == target
    assert not data_root.exists()
    assert not target.exists()


def test_list_and_get_are_stable(tmp_path: Path) -> None:
    resources = (
        StorageResourceRegistration(
            resource_id="logs.system",
            profile_id="system",
            path_template="logs",
        ),
    )
    registry = StorageRegistry(
        profiles=_profiles(tmp_path),
        resources=resources,
    )

    assert tuple(p.profile_id for p in registry.list_profiles()) == (
        "system",
        "local_data",
    )
    assert tuple(r.resource_id for r in registry.list_resources()) == (
        "logs.system",
    )
    assert registry.get_profile("system").profile_id == "system"
