"""Focused contract tests for the shared SettingsManager."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from shane_common.preferences.manager import SettingsManager
from shane_common.preferences.models import SettingDefinition, SettingsCategory


def _make_manager(tmp_path: Path) -> SettingsManager:
    return SettingsManager(path=tmp_path / "settings.yaml")


def _test_category() -> SettingsCategory:
    return SettingsCategory(
        category_id="test.cat",
        label="Test",
        definitions=[
            SettingDefinition(key="alpha", type="int", default=0),
            SettingDefinition(key="beta", type="str", default="hello"),
            SettingDefinition(key="items", type="list", default=[]),
        ],
    )


class TestSettingsManagerConstruction:
    def test_requires_exactly_one_path_or_app_id(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="exactly one"):
            SettingsManager()

        with pytest.raises(ValueError, match="exactly one"):
            SettingsManager(app_id="trading_platform", path=tmp_path / "settings.yaml")

    def test_accepts_explicit_path(self, tmp_path: Path) -> None:
        manager = SettingsManager(path=tmp_path / "settings.yaml")

        assert manager.category_ids() == []


class TestSettingsManagerRegistration:
    def test_duplicate_category_fails_loudly(self, tmp_path: Path) -> None:
        manager = _make_manager(tmp_path)
        category = _test_category()
        manager.register_category(category)

        with pytest.raises(ValueError, match="already registered"):
            manager.register_category(category)

    def test_top_level_shadowing_guard_remains(self, tmp_path: Path) -> None:
        manager = _make_manager(tmp_path)

        with pytest.raises(ValueError, match="shadow"):
            manager.register_category(
                SettingsCategory(
                    category_id="get.general",
                    label="Bad",
                    definitions=[SettingDefinition(key="alpha", type="int", default=0)],
                )
            )


class TestSettingsManagerPersistence:
    def test_load_merges_disk_overrides(self, tmp_path: Path) -> None:
        manager = _make_manager(tmp_path)
        manager.register_category(_test_category())
        manager._path.write_text(
            yaml.safe_dump({"test.cat": {"alpha": 42}}),
            encoding="utf-8",
        )

        manager.load()

        assert manager.get("test.cat", "alpha") == 42
        assert manager.get("test.cat", "beta") == "hello"

    def test_save_persists_only_overrides(self, tmp_path: Path) -> None:
        manager = _make_manager(tmp_path)
        manager.register_category(_test_category())
        manager.set("test.cat", "alpha", 9)

        manager.save()

        assert yaml.safe_load(manager._path.read_text(encoding="utf-8")) == {
            "test.cat": {"alpha": 9}
        }

    def test_unknown_disk_category_and_key_are_ignored(self, tmp_path: Path) -> None:
        manager = _make_manager(tmp_path)
        manager.register_category(_test_category())
        manager._path.write_text(
            yaml.safe_dump(
                {
                    "future.category": {"value": 1},
                    "test.cat": {"missing": "ignored", "beta": "world"},
                }
            ),
            encoding="utf-8",
        )

        manager.load()

        assert manager.get("test.cat", "beta") == "world"
        with pytest.raises(KeyError):
            manager.get("future.category", "value")

    def test_invalid_disk_value_fails_loudly(self, tmp_path: Path) -> None:
        manager = _make_manager(tmp_path)
        manager.register_category(_test_category())
        manager._path.write_text(
            yaml.safe_dump({"test.cat": {"alpha": "bad"}}),
            encoding="utf-8",
        )

        with pytest.raises(ValueError, match="disk"):
            manager.load()

    def test_unknown_on_disk_categories_are_preserved_on_save(self, tmp_path: Path) -> None:
        manager = _make_manager(tmp_path)
        manager.register_category(_test_category())
        manager._path.write_text(
            yaml.safe_dump({"future.category": {"value": 7}}),
            encoding="utf-8",
        )
        manager.set("test.cat", "alpha", 5)

        manager.save()

        assert yaml.safe_load(manager._path.read_text(encoding="utf-8")) == {
            "future.category": {"value": 7},
            "test.cat": {"alpha": 5},
        }


class TestSettingsManagerAccessors:
    def test_dot_access_works_for_namespace_and_category(self, tmp_path: Path) -> None:
        manager = _make_manager(tmp_path)
        manager.register_category(_test_category())

        assert manager.test.cat.alpha == 0

    def test_get_returns_list_copy(self, tmp_path: Path) -> None:
        manager = _make_manager(tmp_path)
        manager.register_category(_test_category())
        items = manager.get("test.cat", "items")
        items.append("x")

        assert manager.get("test.cat", "items") == []

    def test_reset_helpers_restore_defaults(self, tmp_path: Path) -> None:
        manager = _make_manager(tmp_path)
        manager.register_category(_test_category())
        manager.register_category(
            SettingsCategory(
                category_id="other.cat",
                label="Other",
                definitions=[SettingDefinition(key="beta", type="str", default="x")],
            )
        )
        manager.set("test.cat", "alpha", 8)
        manager.set("other.cat", "beta", "changed")

        manager.reset_category("test.cat")
        assert manager.get("test.cat", "alpha") == 0
        assert manager.get("other.cat", "beta") == "changed"

        manager.reset_all()
        assert manager.get("other.cat", "beta") == "x"


class TestSettingsManagerCallbacks:
    def test_subscribe_fires_with_correct_value(self, tmp_path: Path) -> None:
        manager = _make_manager(tmp_path)
        manager.register_category(_test_category())
        received: list[int] = []
        manager.subscribe("test.cat", "alpha", received.append)

        manager.set("test.cat", "alpha", 42)

        assert received == [42]

    def test_unsubscribe_stops_delivery(self, tmp_path: Path) -> None:
        manager = _make_manager(tmp_path)
        manager.register_category(_test_category())
        received: list[int] = []
        callback = received.append
        manager.subscribe("test.cat", "alpha", callback)
        manager.set("test.cat", "alpha", 1)
        manager.unsubscribe("test.cat", "alpha", callback)

        manager.set("test.cat", "alpha", 2)

        assert received == [1]

    def test_exception_in_one_callback_does_not_block_others(self, tmp_path: Path) -> None:
        manager = _make_manager(tmp_path)
        manager.register_category(_test_category())
        received: list[int] = []

        def bad_callback(value: int) -> None:
            raise RuntimeError(f"bad {value}")

        manager.subscribe("test.cat", "alpha", bad_callback)
        manager.subscribe("test.cat", "alpha", received.append)

        manager.set("test.cat", "alpha", 7)

        assert received == [7]

    def test_duplicate_subscription_is_idempotent(self, tmp_path: Path) -> None:
        manager = _make_manager(tmp_path)
        manager.register_category(_test_category())
        received: list[int] = []
        callback = received.append
        manager.subscribe("test.cat", "alpha", callback)
        manager.subscribe("test.cat", "alpha", callback)

        manager.set("test.cat", "alpha", 5)

        assert received == [5]

    def test_no_callback_when_value_is_unchanged(self, tmp_path: Path) -> None:
        manager = _make_manager(tmp_path)
        manager.register_category(_test_category())
        received: list[int] = []
        manager.subscribe("test.cat", "alpha", received.append)

        manager.set("test.cat", "alpha", 0)

        assert received == []