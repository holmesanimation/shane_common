"""Generic semantic storage registry and safe filesystem resolution."""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Iterable, Mapping
from urllib.parse import quote

from .location import StorageLocation
from .profile import (
    StorageConfigurationError,
    StorageProfile,
    is_absolute_filesystem_path,
    validate_identifier,
)


class UnknownStorageProfileError(StorageConfigurationError):
    """Raised when a requested/referenced storage profile does not exist."""


class UnknownStorageResourceError(StorageConfigurationError):
    """Raised when a semantic storage resource is unknown."""


class StorageResolutionError(ValueError):
    """Raised when a valid registry cannot resolve a specific request."""


class StorageTemplateError(StorageResolutionError):
    """Raised when a resource path template or its parameters are unsafe/invalid."""


@dataclass(frozen=True)
class StorageResourceRegistration:
    """Domain-neutral mapping from semantic resource ID to storage profile/path."""

    resource_id: str
    profile_id: str
    path_template: str

    def __post_init__(self) -> None:
        validate_identifier(self.resource_id, field_name="resource_id")
        validate_identifier(self.profile_id, field_name="profile_id")

        if not isinstance(self.path_template, str):
            raise StorageConfigurationError("path_template must be a string")

        _validate_path_template(self.path_template)


class StorageRegistry:
    """Immutable semantic storage-routing registry."""

    def __init__(
        self,
        *,
        profiles: Iterable[StorageProfile],
        resources: Iterable[StorageResourceRegistration],
    ) -> None:
        profile_map: dict[str, StorageProfile] = {}
        for profile in profiles:
            if profile.profile_id in profile_map:
                raise StorageConfigurationError(
                    f"Duplicate storage profile_id {profile.profile_id!r}"
                )
            profile_map[profile.profile_id] = profile

        resource_map: dict[str, StorageResourceRegistration] = {}
        for resource in resources:
            if resource.resource_id in resource_map:
                raise StorageConfigurationError(
                    f"Duplicate storage resource_id {resource.resource_id!r}"
                )
            if resource.profile_id not in profile_map:
                raise UnknownStorageProfileError(
                    f"Storage resource {resource.resource_id!r} references unknown "
                    f"profile {resource.profile_id!r}"
                )
            resource_map[resource.resource_id] = resource

        self._profiles: Mapping[str, StorageProfile] = MappingProxyType(profile_map)
        self._resources: Mapping[str, StorageResourceRegistration] = MappingProxyType(
            resource_map
        )

    def list_profiles(self) -> tuple[StorageProfile, ...]:
        return tuple(self._profiles.values())

    def list_resources(self) -> tuple[StorageResourceRegistration, ...]:
        return tuple(self._resources.values())

    def get_profile(self, profile_id: str) -> StorageProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as exc:
            raise UnknownStorageProfileError(
                f"Unknown storage profile {profile_id!r}"
            ) from exc

    def resolve(self, resource_id: str, **parameters: object) -> StorageLocation:
        try:
            registration = self._resources[resource_id]
        except KeyError as exc:
            raise UnknownStorageResourceError(
                f"Unknown storage resource {resource_id!r}"
            ) from exc

        profile = self.get_profile(registration.profile_id)

        if profile.backend_type != "filesystem":
            # Defensive: WP1 profile validation already limits Stage 1 to filesystem.
            raise StorageResolutionError(
                f"Storage backend {profile.backend_type!r} does not support "
                "filesystem resolution in this implementation"
            )

        relative_path = _render_relative_path(
            registration.path_template,
            parameters,
        )
        local_path = _safe_join(profile.root, relative_path)

        return StorageLocation(
            resource_id=registration.resource_id,
            profile_id=profile.profile_id,
            backend_type=profile.backend_type,
            uri=_filesystem_uri(local_path),
            local_path=local_path,
            durable=profile.durable,
            cacheable=profile.cacheable,
        )

    def resolve_path(self, resource_id: str, **parameters: object) -> Path:
        location = self.resolve(resource_id, **parameters)
        if location.local_path is None:
            raise StorageResolutionError(
                f"Storage resource {resource_id!r} has no local filesystem path"
            )
        return location.local_path


def _validate_path_template(template: str) -> None:
    if "\x00" in template:
        raise StorageConfigurationError("path_template must not contain NUL bytes")

    # Storage resource templates are portable relative paths. Use "/" as the
    # configuration separator and reject "\" to avoid Windows escape ambiguity.
    if "\\" in template:
        raise StorageConfigurationError(
            f"path_template must use '/' separators, not '\\\\': {template!r}"
        )

    pure = PurePosixPath(template or ".")
    if pure.is_absolute():
        raise StorageConfigurationError(
            f"path_template must be relative: {template!r}"
        )
    if any(part == ".." for part in pure.parts):
        raise StorageConfigurationError(
            f"path_template must not contain parent traversal: {template!r}"
        )

    formatter = string.Formatter()
    try:
        parsed = list(formatter.parse(template))
    except ValueError as exc:
        raise StorageConfigurationError(
            f"Invalid path_template {template!r}: {exc}"
        ) from exc

    for _, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        if not field_name:
            raise StorageConfigurationError(
                f"path_template contains an empty placeholder: {template!r}"
            )
        if "." in field_name or "[" in field_name or "]" in field_name:
            raise StorageConfigurationError(
                "path_template placeholders must be simple names"
            )
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field_name):
            raise StorageConfigurationError(
                f"Invalid path_template placeholder {field_name!r}"
            )
        if format_spec or conversion:
            raise StorageConfigurationError(
                "path_template format specs/conversions are not supported"
            )


def _template_fields(template: str) -> set[str]:
    return {
        field_name
        for _, field_name, _, _ in string.Formatter().parse(template)
        if field_name is not None
    }


def _render_relative_path(
    template: str,
    parameters: Mapping[str, object],
) -> PurePosixPath:
    expected = _template_fields(template)
    supplied = set(parameters)

    missing = expected - supplied
    extra = supplied - expected

    if missing:
        raise StorageTemplateError(
            f"Missing template parameter(s): {', '.join(sorted(missing))}"
        )
    if extra:
        raise StorageTemplateError(
            f"Unexpected template parameter(s): {', '.join(sorted(extra))}"
        )

    safe_values: dict[str, str] = {}
    for name, raw in parameters.items():
        if raw is None:
            raise StorageTemplateError(
                f"Template parameter {name!r} must not be None"
            )
        value = str(raw)
        if not value:
            raise StorageTemplateError(
                f"Template parameter {name!r} must not be empty"
            )
        if "\x00" in value or "/" in value or "\\" in value:
            raise StorageTemplateError(
                f"Template parameter {name!r} contains a path separator "
                "or unsafe character"
            )
        if value in {".", ".."}:
            raise StorageTemplateError(
                f"Template parameter {name!r} may not be {value!r}"
            )
        # A Windows drive-qualified value such as C: is not a path segment.
        if PureWindowsPath(value).drive:
            raise StorageTemplateError(
                f"Template parameter {name!r} must not be drive-qualified"
            )
        safe_values[name] = value

    try:
        rendered = template.format(**safe_values)
    except (KeyError, ValueError) as exc:
        raise StorageTemplateError(
            f"Unable to render storage path template {template!r}: {exc}"
        ) from exc

    pure = PurePosixPath(rendered or ".")
    if pure.is_absolute() or any(part == ".." for part in pure.parts):
        raise StorageTemplateError(
            f"Rendered storage path is not a safe relative path: {rendered!r}"
        )
    return pure


def _safe_join(root: Path, relative_path: PurePosixPath) -> Path:
    parts = tuple(part for part in relative_path.parts if part not in {"", "."})
    candidate = root.joinpath(*parts)

    # On the deployment host, Path.resolve(strict=False) provides the most
    # reliable containment check. For a Windows path inspected on a non-Windows
    # host, PureWindowsPath gives a lexical containment check instead.
    root_text = str(root)
    if PureWindowsPath(root_text).is_absolute() and not Path(root_text).is_absolute():
        root_pure = PureWindowsPath(root_text)
        candidate_pure = root_pure.joinpath(*parts)
        try:
            candidate_pure.relative_to(root_pure)
        except ValueError as exc:
            raise StorageTemplateError(
                f"Resolved path escapes storage root {root}"
            ) from exc
        return Path(str(candidate_pure))

    root_resolved = root.resolve(strict=False)
    candidate_resolved = candidate.resolve(strict=False)
    try:
        candidate_resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise StorageTemplateError(
            f"Resolved path escapes storage root {root}"
        ) from exc
    return candidate


def _filesystem_uri(path: Path) -> str:
    value = str(path)
    if PureWindowsPath(value).is_absolute():
        win = PureWindowsPath(value)
        # PureWindowsPath.as_uri() handles drive-letter and UNC syntax.
        return win.as_uri()

    if not is_absolute_filesystem_path(path):
        raise StorageResolutionError(
            f"Cannot create filesystem URI from non-absolute path {path}"
        )

    # pathlib.Path.as_uri() is correct for host-native absolute paths.
    try:
        return path.as_uri()
    except ValueError:
        # Defensive fallback for unusual absolute path implementations.
        return "file://" + quote(value.replace("\\", "/"))
