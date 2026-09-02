"""Tests for generic StorageProfile configuration semantics."""

from pathlib import Path

import pytest

from shane_common.runtime.storage.profile import (
    StorageConfigurationError,
    StorageProfile,
)


def test_valid_filesystem_profile(tmp_path: Path) -> None:
    root = tmp_path / "data"
    profile = StorageProfile(
        profile_id="local_data",
        backend_type="filesystem",
        root=root,
    )

    assert profile.root == root
    assert profile.durable is True
    assert profile.cacheable is False


def test_empty_profile_id_rejected(tmp_path: Path) -> None:
    with pytest.raises(StorageConfigurationError, match="profile_id"):
        StorageProfile(profile_id="", backend_type="filesystem", root=tmp_path)


def test_invalid_profile_id_rejected(tmp_path: Path) -> None:
    with pytest.raises(StorageConfigurationError, match="profile_id"):
        StorageProfile(
            profile_id="Local Data",
            backend_type="filesystem",
            root=tmp_path,
        )


def test_relative_root_rejected() -> None:
    with pytest.raises(StorageConfigurationError, match="absolute"):
        StorageProfile(
            profile_id="local_data",
            backend_type="filesystem",
            root=Path("relative/data"),
        )


def test_nonexistent_absolute_root_is_valid_configuration(tmp_path: Path) -> None:
    root = tmp_path / "does-not-exist"
    assert not root.exists()

    profile = StorageProfile(
        profile_id="archive",
        backend_type="filesystem",
        root=root,
    )

    assert profile.root == root
    assert not root.exists()


def test_windows_absolute_root_is_recognized() -> None:
    profile = StorageProfile(
        profile_id="local_data",
        backend_type="filesystem",
        root=Path("D:/Trading/Data"),
    )
    assert str(profile.root).replace("\\", "/") == "D:/Trading/Data"


def test_unsupported_backend_rejected(tmp_path: Path) -> None:
    with pytest.raises(StorageConfigurationError, match="Unsupported"):
        StorageProfile(
            profile_id="archive",
            backend_type="object_store",
            root=tmp_path,
        )
