from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from shane_common.runtime.storage.filesystem import (
    FilesystemCapacityProbe,
    FilesystemRootProbe,
    FilesystemStorageObserver,
    PortableFilesystemProbe,
    WindowsFilesystemProbe,
)
from shane_common.runtime.storage.observation import (
    AvailabilityStatus,
    ObservationDiagnosticCode,
    WritabilityStatus,
)
from shane_common.runtime.storage.volume import StorageVolume


NOW = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Profile:
    profile_id: str
    backend_type: str
    root: Path


class FakeProbe:
    def __init__(
        self,
        root_results: dict[Path, FilesystemRootProbe],
        capacity_results: dict[str, FilesystemCapacityProbe],
    ) -> None:
        self.root_results = root_results
        self.capacity_results = capacity_results
        self.capacity_calls: list[str] = []

    def inspect_root(self, root: Path) -> FilesystemRootProbe:
        return self.root_results[root]

    def measure_capacity(self, volume: StorageVolume) -> FilesystemCapacityProbe:
        self.capacity_calls.append(volume.volume_id)
        return self.capacity_results[volume.volume_id]


def _volume(volume_id: str, mount: str = "/") -> StorageVolume:
    return StorageVolume(volume_id=volume_id, mount_point=Path(mount))


def test_two_profiles_on_same_volume_are_deduplicated() -> None:
    volume = _volume("volume-d")
    a = Path("/data/a")
    b = Path("/data/b")
    probe = FakeProbe(
        root_results={
            a: FilesystemRootProbe(a, True, True, True, volume),
            b: FilesystemRootProbe(b, True, True, True, volume),
        },
        capacity_results={
            volume.volume_id: FilesystemCapacityProbe(1000, 400, 600),
        },
    )
    observer = FilesystemStorageObserver(probe=probe, clock=lambda: NOW)

    snapshot = observer.observe_profiles(
        [
            Profile("a", "filesystem", a),
            Profile("b", "filesystem", b),
        ]
    )

    assert len(snapshot.profile_observations) == 2
    assert len(snapshot.volumes) == 1
    assert len(snapshot.capacity_observations) == 1
    assert probe.capacity_calls == ["volume-d"]


def test_different_volumes_are_not_deduplicated() -> None:
    a = Path("/data/a")
    b = Path("/archive")
    volume_a = _volume("volume-a", "/")
    volume_b = _volume("volume-b", "/archive")
    probe = FakeProbe(
        root_results={
            a: FilesystemRootProbe(a, True, True, True, volume_a),
            b: FilesystemRootProbe(b, True, True, True, volume_b),
        },
        capacity_results={
            "volume-a": FilesystemCapacityProbe(1000, 100, 900),
            "volume-b": FilesystemCapacityProbe(2000, 500, 1500),
        },
    )

    snapshot = FilesystemStorageObserver(
        probe=probe, clock=lambda: NOW
    ).observe_profiles(
        [
            Profile("a", "filesystem", a),
            Profile("b", "filesystem", b),
        ]
    )

    assert {item.volume_id for item in snapshot.volumes} == {
        "volume-a",
        "volume-b",
    }
    assert probe.capacity_calls == ["volume-a", "volume-b"]


def test_missing_profile_root_is_unavailable_and_does_not_crash() -> None:
    root = Path("/missing")
    probe = FakeProbe(
        root_results={
            root: FilesystemRootProbe(
                root=root,
                exists=False,
                accessible=False,
                writable=None,
                volume=None,
                diagnostic_code=ObservationDiagnosticCode.ROOT_NOT_FOUND,
            )
        },
        capacity_results={},
    )

    observation = FilesystemStorageObserver(
        probe=probe, clock=lambda: NOW
    ).observe_profile(Profile("archive", "filesystem", root))

    assert observation.availability_status is AvailabilityStatus.UNAVAILABLE
    assert observation.writability_status is WritabilityStatus.UNKNOWN
    assert observation.volume_id is None
    assert observation.diagnostic_code is ObservationDiagnosticCode.ROOT_NOT_FOUND


def test_read_only_is_distinct_from_unavailable() -> None:
    root = Path("/readonly")
    volume = _volume("volume-ro")
    probe = FakeProbe(
        root_results={
            root: FilesystemRootProbe(root, True, True, False, volume)
        },
        capacity_results={},
    )

    observation = FilesystemStorageObserver(
        probe=probe, clock=lambda: NOW
    ).observe_profile(Profile("readonly", "filesystem", root))

    assert observation.availability_status is AvailabilityStatus.READ_ONLY
    assert observation.writability_status is WritabilityStatus.READ_ONLY


def test_unknown_writability_is_degraded_not_guessed() -> None:
    root = Path("/ambiguous")
    volume = _volume("volume-a")
    probe = FakeProbe(
        root_results={
            root: FilesystemRootProbe(
                root,
                True,
                True,
                None,
                volume,
                ObservationDiagnosticCode.WRITABILITY_UNKNOWN,
            )
        },
        capacity_results={},
    )

    observation = FilesystemStorageObserver(
        probe=probe, clock=lambda: NOW
    ).observe_profile(Profile("ambiguous", "filesystem", root))

    assert observation.availability_status is AvailabilityStatus.DEGRADED
    assert observation.writability_status is WritabilityStatus.UNKNOWN


def test_capacity_failure_does_not_change_profile_availability() -> None:
    root = Path("/data")
    volume = _volume("volume-a")
    probe = FakeProbe(
        root_results={
            root: FilesystemRootProbe(root, True, True, True, volume)
        },
        capacity_results={
            "volume-a": FilesystemCapacityProbe(
                None,
                None,
                None,
                ObservationDiagnosticCode.CAPACITY_QUERY_FAILED,
                "capacity failed",
            )
        },
    )

    snapshot = FilesystemStorageObserver(
        probe=probe, clock=lambda: NOW
    ).observe_profiles([Profile("data", "filesystem", root)])

    profile_observation = snapshot.profile_observations[0]
    capacity_observation = snapshot.capacity_observations[0]

    assert profile_observation.availability_status is AvailabilityStatus.AVAILABLE
    assert capacity_observation.capacity_bytes is None
    assert capacity_observation.percent_used is None
    assert (
        capacity_observation.diagnostic_code
        is ObservationDiagnosticCode.CAPACITY_QUERY_FAILED
    )


def test_volume_identity_failure_does_not_guess_volume() -> None:
    root = Path("/data")
    probe = FakeProbe(
        root_results={
            root: FilesystemRootProbe(
                root,
                True,
                True,
                True,
                None,
                ObservationDiagnosticCode.VOLUME_IDENTITY_FAILED,
            )
        },
        capacity_results={},
    )

    snapshot = FilesystemStorageObserver(
        probe=probe, clock=lambda: NOW
    ).observe_profiles([Profile("data", "filesystem", root)])

    assert snapshot.profile_observations[0].volume_id is None
    assert snapshot.volumes == ()
    assert snapshot.capacity_observations == ()


def test_non_filesystem_profile_is_programmer_or_composition_error() -> None:
    observer = FilesystemStorageObserver(probe=FakeProbe({}, {}), clock=lambda: NOW)

    with pytest.raises(ValueError, match="backend_type='filesystem'"):
        observer.observe_profile(Profile("cloud", "object_store", Path("/x")))


def test_conflicting_metadata_for_same_volume_id_fails_visible() -> None:
    a = Path("/a")
    b = Path("/b")
    probe = FakeProbe(
        root_results={
            a: FilesystemRootProbe(a, True, True, True, _volume("same", "/one")),
            b: FilesystemRootProbe(b, True, True, True, _volume("same", "/two")),
        },
        capacity_results={},
    )

    with pytest.raises(RuntimeError, match="conflicting StorageVolume metadata"):
        FilesystemStorageObserver(
            probe=probe, clock=lambda: NOW
        ).observe_profiles(
            [
                Profile("a", "filesystem", a),
                Profile("b", "filesystem", b),
            ]
        )


def test_portable_probe_does_not_create_missing_root(tmp_path: Path) -> None:
    missing = tmp_path / "not-created"

    result = PortableFilesystemProbe().inspect_root(missing)

    assert result.exists is False
    assert result.diagnostic_code is ObservationDiagnosticCode.ROOT_NOT_FOUND
    assert not missing.exists()


def test_portable_real_filesystem_smoke_deduplicates_two_directories(
    tmp_path: Path,
) -> None:
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    observer = FilesystemStorageObserver(
        probe=PortableFilesystemProbe(),
        clock=lambda: NOW,
    )
    snapshot = observer.observe_profiles(
        [
            Profile("a", "filesystem", a),
            Profile("b", "filesystem", b),
        ]
    )

    assert len(snapshot.volumes) == 1
    assert len(snapshot.capacity_observations) == 1
    capacity = snapshot.capacity_observations[0]
    assert capacity.capacity_bytes is not None
    assert capacity.capacity_bytes > 0
    assert capacity.free_bytes is not None
    assert capacity.free_bytes >= 0


def test_unc_share_root_normalization() -> None:
    assert (
        WindowsFilesystemProbe._unc_share_root(
            r"\\Server\Share\Trading\Data\\"
        ).lower()
        == "\\\\server\\share\\"
    )
