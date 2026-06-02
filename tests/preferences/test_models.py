"""Focused contract tests for shared preferences schema models."""

import pytest

from shane_common.preferences.models import SettingDefinition, SettingsCategory


def test_setting_definition_rejects_invalid_key() -> None:
    with pytest.raises(ValueError, match="not accessor-safe"):
        SettingDefinition(key="not-valid", type="str", default="x")


def test_setting_definition_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="unknown type"):
        SettingDefinition(key="alpha", type="path", default="x")


def test_setting_definition_rejects_invalid_default_for_enum() -> None:
    with pytest.raises(ValueError, match="is not in choices"):
        SettingDefinition(
            key="alpha",
            type="enum",
            default="c",
            choices=["a", "b"],
        )


def test_settings_category_rejects_duplicate_keys() -> None:
    with pytest.raises(ValueError, match="duplicate key"):
        SettingsCategory(
            category_id="app.general",
            label="General",
            definitions=[
                SettingDefinition(key="alpha", type="int", default=1),
                SettingDefinition(key="alpha", type="int", default=2),
            ],
        )


def test_settings_category_exposes_keys_in_order() -> None:
    category = SettingsCategory(
        category_id="app.general",
        label="General",
        definitions=[
            SettingDefinition(key="alpha", type="int", default=1),
            SettingDefinition(key="beta", type="str", default="x"),
        ],
    )

    assert category.keys == ["alpha", "beta"]