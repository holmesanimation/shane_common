"""Shared runtime settings manager with typed schemas and YAML persistence."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

import yaml

from shane_common.io.atomic import write_text_atomic
from shane_common.preferences.models import SettingsCategory
from shane_common.preferences.paths import app_settings_path

logger = logging.getLogger(__name__)


class _CategoryProxy:
    __slots__ = ("_manager", "_category_id")

    def __init__(self, manager: SettingsManager, category_id: str) -> None:
        object.__setattr__(self, "_manager", manager)
        object.__setattr__(self, "_category_id", category_id)

    def __getattr__(self, key: str) -> Any:
        manager = object.__getattribute__(self, "_manager")
        category_id = object.__getattribute__(self, "_category_id")
        return manager.get(category_id, key)

    def __repr__(self) -> str:
        category_id = object.__getattribute__(self, "_category_id")
        return f"<_CategoryProxy category={category_id!r}>"


class _NamespaceProxy:
    __slots__ = ("_manager", "_prefix")

    def __init__(self, manager: SettingsManager, prefix: str) -> None:
        object.__setattr__(self, "_manager", manager)
        object.__setattr__(self, "_prefix", prefix)

    def __getattr__(self, name: str) -> Any:
        manager = object.__getattribute__(self, "_manager")
        prefix = object.__getattribute__(self, "_prefix")
        next_id = f"{prefix}.{name}"
        categories: dict[str, SettingsCategory] = object.__getattribute__(
            manager, "_categories"
        )

        if next_id in categories:
            return _CategoryProxy(manager, next_id)

        for category_id in categories:
            if category_id.startswith(next_id + "."):
                return _NamespaceProxy(manager, next_id)

        raise AttributeError(f"No settings category or namespace {next_id!r}")

    def __repr__(self) -> str:
        prefix = object.__getattribute__(self, "_prefix")
        return f"<_NamespaceProxy prefix={prefix!r}>"


class SettingsManager:
    def __init__(
        self,
        *,
        app_id: str | None = None,
        path: str | Path | None = None,
    ) -> None:
        if (app_id is None) == (path is None):
            raise ValueError("SettingsManager requires exactly one of app_id or path")

        self._categories: dict[str, SettingsCategory] = {}
        self._effective: dict[str, dict[str, Any]] = {}
        self._subscribers: dict[tuple[str, str], list[Callable[[Any], None]]] = {}
        self._path = Path(path) if path is not None else app_settings_path(app_id)

    def register_category(self, category: SettingsCategory) -> None:
        category_id = category.category_id
        if category_id in self._categories:
            raise ValueError(
                f"SettingsManager: category {category_id!r} is already registered. "
                "Implicit replacement is not allowed; use a distinct category_id."
            )

        top_token = category_id.split(".")[0]
        if hasattr(SettingsManager, top_token) or top_token in vars(self):
            raise ValueError(
                f"SettingsManager: category top-level token {top_token!r} "
                f"(from category {category_id!r}) would shadow an existing "
                "SettingsManager attribute or method. Choose a different "
                "top-level namespace token."
            )

        self._categories[category_id] = category
        self._effective[category_id] = {
            definition.key: definition.default for definition in category.definitions
        }

    def load(self) -> None:
        if not self._path.exists():
            return

        raw = self._read_yaml_mapping(self._path)
        for category_id, category_values in raw.items():
            if category_id not in self._categories:
                continue

            if not isinstance(category_values, dict):
                raise ValueError(
                    f"settings.yaml: category {category_id!r} must be a YAML "
                    f"mapping, got {type(category_values).__name__}"
                )

            category = self._categories[category_id]
            for key, value in category_values.items():
                try:
                    definition = category.get_definition(key)
                except KeyError:
                    continue

                definition._validate_value(
                    value,
                    context=f"disk[{category_id!r}][{key!r}]",
                )
                self._effective[category_id][key] = value

    def save(self) -> None:
        payload: dict[str, dict[str, Any]] = {}

        for category_id, effective in self._effective.items():
            category = self._categories[category_id]
            overrides = {
                key: value
                for key, value in effective.items()
                if value != category.get_definition(key).default
            }
            if overrides:
                payload[category_id] = overrides

        if self._path.exists():
            try:
                existing = self._read_yaml_mapping(self._path)
            except Exception:
                logger.warning(
                    "SettingsManager: failed reading existing settings file during save; "
                    "continuing without unknown-category preservation.",
                    exc_info=True,
                )
            else:
                for category_id, category_values in existing.items():
                    if category_id not in self._categories and isinstance(category_values, dict):
                        payload[category_id] = category_values

        text = yaml.safe_dump(
            payload,
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=True,
        )
        write_text_atomic(self._path, text)

    def get(self, category_id: str, key: str) -> Any:
        try:
            value = self._effective[category_id][key]
            return list(value) if isinstance(value, list) else value
        except KeyError:
            if category_id not in self._categories:
                raise KeyError(f"SettingsManager: unknown category {category_id!r}")
            raise KeyError(
                f"SettingsManager: unknown key {key!r} in category {category_id!r}"
            )

    def get_default(self, category_id: str, key: str) -> Any:
        try:
            category = self._categories[category_id]
        except KeyError:
            raise KeyError(f"SettingsManager: unknown category {category_id!r}")
        return category.get_definition(key).default

    def is_overridden(self, category_id: str, key: str) -> bool:
        return self.get(category_id, key) != self.get_default(category_id, key)

    def set(self, category_id: str, key: str, value: Any) -> None:
        try:
            category = self._categories[category_id]
        except KeyError:
            raise KeyError(f"SettingsManager: unknown category {category_id!r}")

        definition = category.get_definition(key)
        definition._validate_value(value, context=f"set[{category_id!r}][{key!r}]")
        if self._effective[category_id][key] == value:
            return

        self._effective[category_id][key] = value
        self._notify(category_id, key, value)

    def subscribe(
        self,
        category_id: str,
        key: str,
        callback: Callable[[Any], None],
    ) -> None:
        slot = self._subscribers.setdefault((category_id, key), [])
        if callback not in slot:
            slot.append(callback)

    def unsubscribe(
        self,
        category_id: str,
        key: str,
        callback: Callable[[Any], None],
    ) -> None:
        slot = self._subscribers.get((category_id, key))
        if slot and callback in slot:
            slot.remove(callback)

    def _notify(self, category_id: str, key: str, value: Any) -> None:
        for callback in list(self._subscribers.get((category_id, key), [])):
            try:
                callback(value)
            except Exception:
                logger.warning(
                    "SettingsManager: subscriber raised an exception for %r.%r; ignoring.",
                    category_id,
                    key,
                    exc_info=True,
                )

    def reset_category(self, category_id: str) -> None:
        if category_id not in self._categories:
            raise KeyError(f"SettingsManager: unknown category {category_id!r}")
        category = self._categories[category_id]
        self._effective[category_id] = {
            definition.key: definition.default for definition in category.definitions
        }

    def reset_all(self) -> None:
        for category_id, category in self._categories.items():
            self._effective[category_id] = {
                definition.key: definition.default for definition in category.definitions
            }

    def categories(self) -> list[SettingsCategory]:
        return list(self._categories.values())

    def category_ids(self) -> list[str]:
        return list(self._categories.keys())

    def __getattr__(self, name: str) -> Any:
        try:
            categories: dict[str, SettingsCategory] = object.__getattribute__(
                self, "_categories"
            )
        except AttributeError:
            raise AttributeError(name)

        if name in categories:
            return _CategoryProxy(self, name)

        for category_id in categories:
            if category_id.split(".")[0] == name:
                return _NamespaceProxy(self, name)

        raise AttributeError(
            f"SettingsManager has no attribute or settings namespace {name!r}"
        )

    @staticmethod
    def _read_yaml_mapping(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"settings.yaml at {path} is malformed: expected a YAML mapping, "
                f"got {type(raw).__name__}"
            )
        return raw