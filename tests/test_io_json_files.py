"""Unit tests for shane_common.io.json_files."""

import json
from pathlib import Path

import pytest

from shane_common.io.json_files import load_json_file, save_json_file_atomic


class TestLoadJsonFile:
    def test_returns_default_when_file_missing(self, tmp_path):
        result = load_json_file(tmp_path / "nonexistent.json")
        assert result is None

    def test_custom_default_when_missing(self, tmp_path):
        result = load_json_file(tmp_path / "nonexistent.json", default={"fallback": True})
        assert result == {"fallback": True}

    def test_returns_parsed_dict(self, tmp_path):
        p = tmp_path / "config.json"
        p.write_text('{"key": "value"}', encoding="utf-8")
        assert load_json_file(p) == {"key": "value"}

    def test_returns_parsed_list(self, tmp_path):
        p = tmp_path / "items.json"
        p.write_text('[1, 2, 3]', encoding="utf-8")
        assert load_json_file(p) == [1, 2, 3]

    def test_returns_default_on_malformed_json(self, tmp_path):
        p = tmp_path / "bad.json"
        p.write_text("{not valid json", encoding="utf-8")
        result = load_json_file(p, default="fallback")
        assert result == "fallback"

    def test_returns_default_on_empty_file(self, tmp_path):
        p = tmp_path / "empty.json"
        p.write_text("", encoding="utf-8")
        assert load_json_file(p) is None


class TestSaveJsonFileAtomic:
    def test_creates_file(self, tmp_path):
        p = tmp_path / "out.json"
        save_json_file_atomic(p, {"x": 1})
        assert p.exists()

    def test_round_trip(self, tmp_path):
        p = tmp_path / "out.json"
        original = {"a": [1, 2], "b": "hello"}
        save_json_file_atomic(p, original)
        assert load_json_file(p) == original

    def test_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "nested" / "config.json"
        save_json_file_atomic(p, {})
        assert p.exists()
