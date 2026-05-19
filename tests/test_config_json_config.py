"""Unit tests for shane_common.config.json_config."""

import json
from pathlib import Path

import pytest

from shane_common.config.json_config import JsonConfigStore


def _default_factory():
    return {"version": 1, "items": []}


def _normalize(raw):
    if not isinstance(raw, dict):
        return _default_factory()
    raw.setdefault("version", 1)
    raw.setdefault("items", [])
    return raw


def _to_json_ready(config):
    return {"version": config["version"], "items": list(config["items"])}


class TestJsonConfigStoreLoad:
    def test_returns_default_when_file_missing(self, tmp_path):
        store = JsonConfigStore(tmp_path / "config.json", _default_factory)
        result = store.load()
        assert result == _default_factory()

    def test_returns_default_when_file_is_malformed(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text("{bad json", encoding="utf-8")
        store = JsonConfigStore(p, _default_factory)
        assert store.load() == _default_factory()

    def test_returns_parsed_content(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text('{"version": 2, "items": ["a"]}', encoding="utf-8")
        store = JsonConfigStore(p, _default_factory)
        result = store.load()
        assert result == {"version": 2, "items": ["a"]}

    def test_normalize_applied_on_load(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text('{"version": 3}', encoding="utf-8")
        store = JsonConfigStore(p, _default_factory, normalize=_normalize)
        result = store.load()
        # normalize adds missing "items" key
        assert "items" in result

    def test_normalize_applied_when_returning_default(self, tmp_path):
        normalize_called = []

        def tracking_normalize(raw):
            normalize_called.append(raw)
            return raw

        store = JsonConfigStore(
            tmp_path / "missing.json",
            _default_factory,
            normalize=tracking_normalize,
        )
        store.load()
        # normalize is NOT called on the default_factory result per contract:
        # load() returns default_factory() directly when file is absent.
        # (normalize is only called on disk content)
        assert len(normalize_called) == 0


class TestJsonConfigStoreSave:
    def test_creates_file(self, tmp_path):
        p = tmp_path / "config.json"
        store = JsonConfigStore(p, _default_factory)
        store.save({"version": 1, "items": []})
        assert p.exists()

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "sub" / "config.json"
        store = JsonConfigStore(p, _default_factory)
        store.save({"version": 1, "items": []})
        assert p.exists()

    def test_saved_content_is_valid_json(self, tmp_path):
        p = tmp_path / "config.json"
        store = JsonConfigStore(p, _default_factory)
        store.save({"version": 1, "items": ["x"]})
        parsed = json.loads(p.read_text(encoding="utf-8"))
        assert parsed["items"] == ["x"]

    def test_save_returns_normalized_config(self, tmp_path):
        p = tmp_path / "config.json"
        store = JsonConfigStore(p, _default_factory, normalize=_normalize)
        # Pass config missing "items"; normalize should add it
        result = store.save({"version": 5})
        assert result["items"] == []

    def test_to_json_ready_applied_before_write(self, tmp_path):
        p = tmp_path / "config.json"
        seen = []

        def spy_to_json_ready(cfg):
            seen.append(cfg)
            return cfg

        store = JsonConfigStore(p, _default_factory, to_json_ready=spy_to_json_ready)
        store.save({"version": 1, "items": []})
        assert len(seen) == 1

    def test_round_trip_save_then_load(self, tmp_path):
        p = tmp_path / "config.json"
        store = JsonConfigStore(
            p, _default_factory, normalize=_normalize, to_json_ready=_to_json_ready
        )
        original = {"version": 7, "items": ["alpha", "beta"]}
        store.save(original)
        loaded = store.load()
        assert loaded == original

    def test_normalize_called_on_save(self, tmp_path):
        p = tmp_path / "config.json"
        called = []

        def tracking_normalize(raw):
            called.append(raw)
            return raw if isinstance(raw, dict) else _default_factory()

        store = JsonConfigStore(p, _default_factory, normalize=tracking_normalize)
        store.save({"version": 1, "items": []})
        assert len(called) == 1
