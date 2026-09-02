"""Filesystem-backed storage observation.

The observer is synchronous and one-shot by design. A later monitoring layer
may call it periodically; this module owns no timer, thread, daemon, polling
cadence, health threshold, fallback policy, or filesystem mutation.

Windows-native volume GUID discovery is isolated behind `WindowsFilesystemProbe`
so the orchestration logic can be tested deterministically with a fake probe.
"""

from __future__ import annotations

import ctypes
import os
import shutil
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Callable, Iterable, Protocol

from .observation import (
    AvailabilityStatus,
    ObservationDiagnosticCode,
    StorageCapacityObservation,
    StorageObservationSnapshot,
    StorageProfileObservation,
    WritabilityStatus,
    utc_now,
)
from .volume import StorageVolume


FILE_READ_ONLY_VOLUME = 0x00080000


@dataclass(frozen=True, slots=True)
class FilesystemRootProbe:
    """Low-level result for one configured filesystem root."""

    root: Path
    exists: bool
    accessible: bool
    writable: bool | None
    volume: StorageVolume | None
    diagnostic_code: ObservationDiagnosticCode | None = None
    diagnostic_message: str | None = None


@dataclass(frozen=True, slots=True)
class FilesystemCapacityProbe:
    """Low-level capacity result for one identified volume."""

    capacity_bytes: int | None
    used_bytes: int | None
    free_bytes: int | None
    diagnostic_code: ObservationDiagnosticCode | None = None
    diagnostic_message: str | None = None


class FilesystemProbe(Protocol):
    """OS-specific seam used by `FilesystemStorageObserver`."""

    def inspect_root(self, root: Path) -> FilesystemRootProbe:
        ...

    def measure_capacity(self, volume: StorageVolume) -> FilesystemCapacityProbe:
        ...


class FilesystemStorageObserver:
    """Observe configured filesystem storage without mutating it."""

    def __init__(
        self,
        probe: FilesystemProbe | None = None,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._probe = probe or default_filesystem_probe()
        self._clock = clock

    def observe_profile(self, profile: object) -> StorageProfileObservation:
        """Observe one StorageProfile-like object.

        The object must expose `profile_id`, `backend_type`, and `root`. Using
        attribute-based access keeps this module additive and avoids changing
        WP1's `StorageProfile` implementation.
        """

        self._validate_filesystem_profile(profile)
        observed_at = self._clock()
        root = Path(getattr(profile, "root"))
        result = self._probe.inspect_root(root)
        return self._to_profile_observation(
            profile_id=str(getattr(profile, "profile_id")),
            root=root,
            result=result,
            observed_at=observed_at,
        )

    def observe_profiles(self, profiles: Iterable[object]) -> StorageObservationSnapshot:
        """Observe profiles and capacity once per unique filesystem volume."""

        observed_at = self._clock()
        profile_observations: list[StorageProfileObservation] = []
        volumes_by_id: dict[str, StorageVolume] = {}

        for profile in profiles:
            self._validate_filesystem_profile(profile)
            root = Path(getattr(profile, "root"))
            result = self._probe.inspect_root(root)

            profile_observations.append(
                self._to_profile_observation(
                    profile_id=str(getattr(profile, "profile_id")),
                    root=root,
                    result=result,
                    observed_at=observed_at,
                )
            )

            if result.volume is not None:
                existing = volumes_by_id.get(result.volume.volume_id)
                if existing is None:
                    volumes_by_id[result.volume.volume_id] = result.volume
                elif existing != result.volume:
                    raise RuntimeError(
                        "filesystem probe returned conflicting StorageVolume metadata "
                        f"for volume_id={result.volume.volume_id!r}"
                    )

        capacity_observations: list[StorageCapacityObservation] = []
        for volume in volumes_by_id.values():
            capacity = self._probe.measure_capacity(volume)
            capacity_observations.append(
                StorageCapacityObservation(
                    volume_id=volume.volume_id,
                    capacity_bytes=capacity.capacity_bytes,
                    used_bytes=capacity.used_bytes,
                    free_bytes=capacity.free_bytes,
                    observed_at_utc=observed_at,
                    diagnostic_code=capacity.diagnostic_code,
                    diagnostic_message=capacity.diagnostic_message,
                )
            )

        return StorageObservationSnapshot(
            profile_observations=tuple(profile_observations),
            volumes=tuple(volumes_by_id.values()),
            capacity_observations=tuple(capacity_observations),
            observed_at_utc=observed_at,
        )

    @staticmethod
    def _validate_filesystem_profile(profile: object) -> None:
        for attribute in ("profile_id", "backend_type", "root"):
            if not hasattr(profile, attribute):
                raise TypeError(f"profile must expose {attribute!r}")

        backend_type = str(getattr(profile, "backend_type")).strip().lower()
        if backend_type != "filesystem":
            raise ValueError(
                "FilesystemStorageObserver only supports backend_type='filesystem'; "
                f"got {getattr(profile, 'backend_type')!r}"
            )

    @staticmethod
    def _to_profile_observation(
        *,
        profile_id: str,
        root: Path,
        result: FilesystemRootProbe,
        observed_at: datetime,
    ) -> StorageProfileObservation:
        if not result.exists:
            availability = AvailabilityStatus.UNAVAILABLE
            writability = WritabilityStatus.UNKNOWN
        elif not result.accessible:
            availability = AvailabilityStatus.UNAVAILABLE
            writability = WritabilityStatus.UNKNOWN
        elif result.writable is True:
            availability = AvailabilityStatus.AVAILABLE
            writability = WritabilityStatus.WRITABLE
        elif result.writable is False:
            availability = AvailabilityStatus.READ_ONLY
            writability = WritabilityStatus.READ_ONLY
        else:
            availability = AvailabilityStatus.DEGRADED
            writability = WritabilityStatus.UNKNOWN

        return StorageProfileObservation(
            profile_id=profile_id,
            root=root,
            availability_status=availability,
            writability_status=writability,
            volume_id=result.volume.volume_id if result.volume else None,
            observed_at_utc=observed_at,
            diagnostic_code=result.diagnostic_code,
            diagnostic_message=result.diagnostic_message,
        )


class PortableFilesystemProbe:
    """Conservative fallback for non-Windows development/tests.

    It intentionally uses the mount point reported by `Path.anchor`/`os.path`
    and does not claim Windows-stable volume GUID identity.
    """

    def inspect_root(self, root: Path) -> FilesystemRootProbe:
        root = Path(root)

        try:
            exists = root.exists()
        except PermissionError as exc:
            return FilesystemRootProbe(
                root=root,
                exists=True,
                accessible=False,
                writable=None,
                volume=None,
                diagnostic_code=ObservationDiagnosticCode.PERMISSION_DENIED,
                diagnostic_message=str(exc),
            )
        except OSError as exc:
            return FilesystemRootProbe(
                root=root,
                exists=False,
                accessible=False,
                writable=None,
                volume=None,
                diagnostic_code=ObservationDiagnosticCode.VOLUME_UNAVAILABLE,
                diagnostic_message=str(exc),
            )

        if not exists:
            return FilesystemRootProbe(
                root=root,
                exists=False,
                accessible=False,
                writable=None,
                volume=None,
                diagnostic_code=ObservationDiagnosticCode.ROOT_NOT_FOUND,
                diagnostic_message=f"Configured storage root does not exist: {root}",
            )

        try:
            resolved = root.resolve(strict=True)
            accessible = True
        except PermissionError as exc:
            return FilesystemRootProbe(
                root=root,
                exists=True,
                accessible=False,
                writable=None,
                volume=None,
                diagnostic_code=ObservationDiagnosticCode.PERMISSION_DENIED,
                diagnostic_message=str(exc),
            )
        except OSError as exc:
            return FilesystemRootProbe(
                root=root,
                exists=True,
                accessible=False,
                writable=None,
                volume=None,
                diagnostic_code=ObservationDiagnosticCode.VOLUME_UNAVAILABLE,
                diagnostic_message=str(exc),
            )

        mount_point = self._find_mount_point(resolved)
        volume = StorageVolume(
            volume_id=f"filesystem:mount:{os.path.normcase(str(mount_point))}",
            mount_point=mount_point,
            filesystem_type=None,
        )

        try:
            writable = os.access(resolved, os.W_OK)
        except OSError:
            writable = None

        return FilesystemRootProbe(
            root=root,
            exists=True,
            accessible=accessible,
            writable=writable,
            volume=volume,
            diagnostic_code=(
                None
                if writable is not None
                else ObservationDiagnosticCode.WRITABILITY_UNKNOWN
            ),
        )

    def measure_capacity(self, volume: StorageVolume) -> FilesystemCapacityProbe:
        try:
            usage = shutil.disk_usage(volume.mount_point)
        except OSError as exc:
            return FilesystemCapacityProbe(
                capacity_bytes=None,
                used_bytes=None,
                free_bytes=None,
                diagnostic_code=ObservationDiagnosticCode.CAPACITY_QUERY_FAILED,
                diagnostic_message=str(exc),
            )
        return FilesystemCapacityProbe(
            capacity_bytes=usage.total,
            used_bytes=usage.used,
            free_bytes=usage.free,
        )

    @staticmethod
    def _find_mount_point(path: Path) -> Path:
        current = path
        while current.parent != current and not os.path.ismount(current):
            current = current.parent
        return current


class WindowsFilesystemProbe:
    """Read-only Windows filesystem probe using Win32 volume APIs."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise RuntimeError("WindowsFilesystemProbe requires Windows")
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_functions()

    def inspect_root(self, root: Path) -> FilesystemRootProbe:
        root = Path(root)

        # A profile root is observed as configured; WP2 never creates it.
        try:
            exists = root.exists()
        except PermissionError as exc:
            return FilesystemRootProbe(
                root=root,
                exists=True,
                accessible=False,
                writable=None,
                volume=None,
                diagnostic_code=ObservationDiagnosticCode.PERMISSION_DENIED,
                diagnostic_message=str(exc),
            )
        except OSError as exc:
            return FilesystemRootProbe(
                root=root,
                exists=False,
                accessible=False,
                writable=None,
                volume=None,
                diagnostic_code=ObservationDiagnosticCode.VOLUME_UNAVAILABLE,
                diagnostic_message=str(exc),
            )

        if not exists:
            return FilesystemRootProbe(
                root=root,
                exists=False,
                accessible=False,
                writable=None,
                volume=None,
                diagnostic_code=ObservationDiagnosticCode.ROOT_NOT_FOUND,
                diagnostic_message=f"Configured storage root does not exist: {root}",
            )

        try:
            resolved = root.resolve(strict=True)
        except PermissionError as exc:
            return FilesystemRootProbe(
                root=root,
                exists=True,
                accessible=False,
                writable=None,
                volume=None,
                diagnostic_code=ObservationDiagnosticCode.PERMISSION_DENIED,
                diagnostic_message=str(exc),
            )
        except OSError as exc:
            return FilesystemRootProbe(
                root=root,
                exists=True,
                accessible=False,
                writable=None,
                volume=None,
                diagnostic_code=ObservationDiagnosticCode.VOLUME_UNAVAILABLE,
                diagnostic_message=str(exc),
            )

        try:
            volume = self._identify_volume(resolved)
        except OSError as exc:
            return FilesystemRootProbe(
                root=root,
                exists=True,
                accessible=True,
                writable=self._safe_os_access(resolved),
                volume=None,
                diagnostic_code=ObservationDiagnosticCode.VOLUME_IDENTITY_FAILED,
                diagnostic_message=str(exc),
            )

        read_only_flag: bool | None
        try:
            read_only_flag = self._volume_is_read_only(volume.mount_point)
        except OSError:
            read_only_flag = None

        access_writable = self._safe_os_access(resolved)
        if read_only_flag is True:
            writable: bool | None = False
            diagnostic_code = None
            diagnostic_message = None
        elif access_writable is False:
            writable = False
            diagnostic_code = None
            diagnostic_message = None
        elif read_only_flag is False and access_writable is True:
            writable = True
            diagnostic_code = None
            diagnostic_message = None
        else:
            writable = None
            diagnostic_code = ObservationDiagnosticCode.WRITABILITY_UNKNOWN
            diagnostic_message = (
                "The filesystem is reachable, but writability could not be classified "
                "without performing a write."
            )

        return FilesystemRootProbe(
            root=root,
            exists=True,
            accessible=True,
            writable=writable,
            volume=volume,
            diagnostic_code=diagnostic_code,
            diagnostic_message=diagnostic_message,
        )

    def measure_capacity(self, volume: StorageVolume) -> FilesystemCapacityProbe:
        free_available = ctypes.c_ulonglong()
        total_bytes = ctypes.c_ulonglong()
        total_free = ctypes.c_ulonglong()

        ok = self._kernel32.GetDiskFreeSpaceExW(
            str(volume.mount_point),
            ctypes.byref(free_available),
            ctypes.byref(total_bytes),
            ctypes.byref(total_free),
        )
        if not ok:
            error = ctypes.get_last_error()
            return FilesystemCapacityProbe(
                capacity_bytes=None,
                used_bytes=None,
                free_bytes=None,
                diagnostic_code=ObservationDiagnosticCode.CAPACITY_QUERY_FAILED,
                diagnostic_message=self._format_win_error(
                    error, f"GetDiskFreeSpaceExW({volume.mount_point}) failed"
                ),
            )

        total = int(total_bytes.value)
        free = int(total_free.value)
        used = total - free
        return FilesystemCapacityProbe(
            capacity_bytes=total,
            used_bytes=used,
            free_bytes=free,
        )

    def _identify_volume(self, path: Path) -> StorageVolume:
        path_text = self._as_windows_directory_path(path)

        if path_text.startswith("\\\\"):
            share_root = self._unc_share_root(path_text)
            filesystem_type = self._filesystem_type(share_root)
            volume_id = f"windows:unc:{share_root[2:].rstrip(chr(92)).lower().replace(chr(92), '/')}"
            return StorageVolume(
                volume_id=volume_id,
                mount_point=Path(share_root),
                filesystem_type=filesystem_type,
            )

        mount_point = self._volume_path_name(path_text)
        filesystem_type = self._filesystem_type(mount_point)

        try:
            volume_guid = self._volume_guid_name(mount_point)
        except OSError:
            # Conservative fallback: mount-root identity can under-deduplicate if Windows
            # identity APIs are unavailable, but it will not merge unrelated mount roots.
            volume_id = f"windows:mount:{mount_point.rstrip(chr(92)).lower()}"
        else:
            canonical_guid = volume_guid.removeprefix("\\\\?\\Volume{").removesuffix("}\\")
            volume_id = f"windows:volume:{canonical_guid.lower()}"

        return StorageVolume(
            volume_id=volume_id,
            mount_point=Path(mount_point),
            filesystem_type=filesystem_type,
        )

    def _volume_path_name(self, path_text: str) -> str:
        buffer = ctypes.create_unicode_buffer(32768)
        ok = self._kernel32.GetVolumePathNameW(
            path_text,
            buffer,
            len(buffer),
        )
        if not ok:
            error = ctypes.get_last_error()
            raise OSError(
                error,
                self._format_win_error(error, "GetVolumePathNameW failed"),
            )
        return buffer.value

    def _volume_guid_name(self, mount_point: str) -> str:
        mount_point = self._ensure_trailing_backslash(mount_point)
        buffer = ctypes.create_unicode_buffer(1024)
        ok = self._kernel32.GetVolumeNameForVolumeMountPointW(
            mount_point,
            buffer,
            len(buffer),
        )
        if not ok:
            error = ctypes.get_last_error()
            raise OSError(
                error,
                self._format_win_error(
                    error, "GetVolumeNameForVolumeMountPointW failed"
                ),
            )
        return buffer.value

    def _filesystem_type(self, mount_point: str) -> str | None:
        mount_point = self._ensure_trailing_backslash(mount_point)
        volume_name = ctypes.create_unicode_buffer(261)
        filesystem_name = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        max_component = wintypes.DWORD()
        flags = wintypes.DWORD()

        ok = self._kernel32.GetVolumeInformationW(
            mount_point,
            volume_name,
            len(volume_name),
            ctypes.byref(serial),
            ctypes.byref(max_component),
            ctypes.byref(flags),
            filesystem_name,
            len(filesystem_name),
        )
        if not ok:
            return None
        return filesystem_name.value or None

    def _volume_is_read_only(self, mount_point: Path) -> bool:
        root = self._ensure_trailing_backslash(str(mount_point))
        volume_name = ctypes.create_unicode_buffer(261)
        filesystem_name = ctypes.create_unicode_buffer(261)
        serial = wintypes.DWORD()
        max_component = wintypes.DWORD()
        flags = wintypes.DWORD()

        ok = self._kernel32.GetVolumeInformationW(
            root,
            volume_name,
            len(volume_name),
            ctypes.byref(serial),
            ctypes.byref(max_component),
            ctypes.byref(flags),
            filesystem_name,
            len(filesystem_name),
        )
        if not ok:
            error = ctypes.get_last_error()
            raise OSError(
                error,
                self._format_win_error(error, "GetVolumeInformationW failed"),
            )
        return bool(flags.value & FILE_READ_ONLY_VOLUME)

    @staticmethod
    def _safe_os_access(path: Path) -> bool | None:
        try:
            return os.access(path, os.W_OK)
        except OSError:
            return None

    @staticmethod
    def _as_windows_directory_path(path: Path) -> str:
        # Win32 volume APIs accept directory paths. A trailing separator also avoids
        # ambiguous drive-relative strings such as "D:".
        return WindowsFilesystemProbe._ensure_trailing_backslash(str(path))

    @staticmethod
    def _ensure_trailing_backslash(value: str) -> str:
        return value if value.endswith("\\") else value + "\\"

    @staticmethod
    def _unc_share_root(path_text: str) -> str:
        path = PureWindowsPath(path_text)
        anchor = path.anchor
        if not anchor.startswith("\\\\"):
            raise ValueError(f"not a UNC path: {path_text!r}")
        return WindowsFilesystemProbe._ensure_trailing_backslash(anchor)

    @staticmethod
    def _format_win_error(error: int, prefix: str) -> str:
        try:
            detail = ctypes.FormatError(error).strip()
        except Exception:
            detail = ""
        return f"{prefix}: [WinError {error}] {detail}".rstrip()

    def _configure_functions(self) -> None:
        self._kernel32.GetVolumePathNameW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        self._kernel32.GetVolumePathNameW.restype = wintypes.BOOL

        self._kernel32.GetVolumeNameForVolumeMountPointW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        self._kernel32.GetVolumeNameForVolumeMountPointW.restype = wintypes.BOOL

        self._kernel32.GetDiskFreeSpaceExW.argtypes = [
            wintypes.LPCWSTR,
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
            ctypes.POINTER(ctypes.c_ulonglong),
        ]
        self._kernel32.GetDiskFreeSpaceExW.restype = wintypes.BOOL

        self._kernel32.GetVolumeInformationW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        self._kernel32.GetVolumeInformationW.restype = wintypes.BOOL


def default_filesystem_probe() -> FilesystemProbe:
    if os.name == "nt":
        return WindowsFilesystemProbe()
    return PortableFilesystemProbe()
