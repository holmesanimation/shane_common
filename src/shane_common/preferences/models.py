"""Pure-Python schema model for shared app preferences."""

from __future__ import annotations

import keyword
from dataclasses import dataclass, field
from typing import Any


VALID_TYPES: frozenset[str] = frozenset(
    {"str", "int", "float", "bool", "list", "dict", "enum"}
)


def _is_accessor_safe(name: str) -> bool:
    return (
        isinstance(name, str)
        and name.isidentifier()
        and not keyword.iskeyword(name)
        and not name.startswith("_")
    )


@dataclass
class SettingDefinition:
    key: str
    type: str
    default: Any
    label: str = ""
    description: str = ""
    choices: list[Any] | None = None
    min: float | int | None = None
    max: float | int | None = None

    def __post_init__(self) -> None:
        if not _is_accessor_safe(self.key):
            raise ValueError(
                f"SettingDefinition key {self.key!r} is not accessor-safe "
                "(must be a valid Python identifier, not a keyword, "
                "and not underscore-prefixed)"
            )

        if self.type not in VALID_TYPES:
            raise ValueError(
                f"SettingDefinition {self.key!r}: unknown type {self.type!r}; "
                f"valid types are {sorted(VALID_TYPES)}"
            )

        if self.type == "enum":
            if not self.choices:
                raise ValueError(
                    f"SettingDefinition {self.key!r}: type='enum' requires "
                    "a non-empty 'choices' list"
                )
        elif self.choices is not None:
            raise ValueError(
                f"SettingDefinition {self.key!r}: 'choices' is only valid "
                "for type='enum'"
            )

        if self.min is not None or self.max is not None:
            if self.type not in ("int", "float"):
                raise ValueError(
                    f"SettingDefinition {self.key!r}: 'min'/'max' are only "
                    "valid for type='int' or type='float'"
                )
            if (
                self.min is not None
                and self.max is not None
                and self.min > self.max
            ):
                raise ValueError(
                    f"SettingDefinition {self.key!r}: min ({self.min}) "
                    f"must not exceed max ({self.max})"
                )

        self._validate_value(self.default, context="default")

    def _validate_value(self, value: Any, *, context: str = "value") -> None:
        if self.type == "bool":
            if not isinstance(value, bool):
                raise ValueError(
                    f"SettingDefinition {self.key!r}: {context} {value!r} "
                    "is not bool"
                )

        elif self.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"SettingDefinition {self.key!r}: {context} {value!r} "
                    "is not int"
                )

        elif self.type == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(
                    f"SettingDefinition {self.key!r}: {context} {value!r} "
                    "is not numeric (int or float)"
                )

        elif self.type == "str":
            if not isinstance(value, str):
                raise ValueError(
                    f"SettingDefinition {self.key!r}: {context} {value!r} "
                    "is not str"
                )

        elif self.type == "list":
            if not isinstance(value, list):
                raise ValueError(
                    f"SettingDefinition {self.key!r}: {context} {value!r} "
                    "is not list"
                )

        elif self.type == "dict":
            if not isinstance(value, dict):
                raise ValueError(
                    f"SettingDefinition {self.key!r}: {context} {value!r} "
                    "is not dict"
                )

        elif self.type == "enum":
            if value not in self.choices:
                raise ValueError(
                    f"SettingDefinition {self.key!r}: {context} {value!r} "
                    f"is not in choices {self.choices!r}"
                )

        if self.type in ("int", "float") and not isinstance(value, bool):
            if self.min is not None and value < self.min:
                raise ValueError(
                    f"SettingDefinition {self.key!r}: {context} {value!r} "
                    f"is below min {self.min}"
                )
            if self.max is not None and value > self.max:
                raise ValueError(
                    f"SettingDefinition {self.key!r}: {context} {value!r} "
                    f"exceeds max {self.max}"
                )


@dataclass
class SettingsCategory:
    category_id: str
    label: str
    definitions: list[SettingDefinition] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.category_id:
            raise ValueError("SettingsCategory: category_id must not be empty")

        for token in self.category_id.split("."):
            if not _is_accessor_safe(token):
                raise ValueError(
                    f"SettingsCategory {self.category_id!r}: token {token!r} "
                    "is not accessor-safe (must be a valid Python identifier, "
                    "not a keyword, and not underscore-prefixed)"
                )

        seen: set[str] = set()
        for defn in self.definitions:
            if defn.key in seen:
                raise ValueError(
                    f"SettingsCategory {self.category_id!r}: "
                    f"duplicate key {defn.key!r}"
                )
            seen.add(defn.key)

    @property
    def keys(self) -> list[str]:
        return [definition.key for definition in self.definitions]

    def get_definition(self, key: str) -> SettingDefinition:
        for definition in self.definitions:
            if definition.key == key:
                return definition
        raise KeyError(
            f"No definition for key {key!r} in category {self.category_id!r}"
        )