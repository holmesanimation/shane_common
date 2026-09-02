"""Generic configured-storage profile primitives."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


_IDENTIFIER_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]*$")
SUPPORTED_STORAGE_BACKENDS = frozenset({"filesystem"})


class StorageConfigurationError(ValueError):
    """Raised when configured storage routing is structurally invalid."""


def is_absolute_filesystem_path(path: str | Path) -> bool:
    """Return True for host-native or Windows absolute filesystem paths.

    The trading deployment is Windows-centric, but the shared package and its
    tests should still be inspectable from other hosts.
    """

    value = str(path)
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def validate_identifier(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise StorageConfigurationError(f"{field_name} must be a non-empty string")
    if not _IDENTIFIER_RE.fullmatch(value):
        raise StorageConfigurationError(
            f"{field_name} {value!r} is invalid; expected pattern "
            "[a-z0-9][a-z0-9_.-]*"
        )
    return value


@dataclass(frozen=True)
class StorageProfile:
    """Configured logical storage destination.

    This is configuration truth only. It deliberately does not probe the
    filesystem and does not contain availability, writability, or capacity
    observations.
    """

    profile_id: str
    backend_type: str
    root: Path
    durable: bool = True
    cacheable: bool = False

    def __post_init__(self) -> None:
        validate_identifier(self.profile_id, field_name="profile_id")

        if self.backend_type not in SUPPORTED_STORAGE_BACKENDS:
            raise StorageConfigurationError(
                f"Unsupported storage backend {self.backend_type!r}; "
                f"supported backends are {sorted(SUPPORTED_STORAGE_BACKENDS)}"
            )

        root = Path(self.root)
        if not is_absolute_filesystem_path(root):
            raise StorageConfigurationError(
                f"Storage profile {self.profile_id!r} root must be absolute: {root}"
            )

        object.__setattr__(self, "root", root)
