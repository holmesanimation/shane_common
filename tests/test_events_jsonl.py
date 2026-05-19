"""Unit tests for shane_common.events.jsonl."""

import datetime
import json
from pathlib import Path

import pytest

from shane_common.events.jsonl import JsonlEventWriter


class TestJsonlEventWriterAppend:
    def test_creates_file_on_first_append(self, tmp_path):
        path = tmp_path / "events.jsonl"
        writer = JsonlEventWriter(lambda: path)
        writer.append({"event": "start"})
        assert path.exists()

    def test_single_record_is_valid_json_line(self, tmp_path):
        path = tmp_path / "events.jsonl"
        writer = JsonlEventWriter(lambda: path)
        writer.append({"event": "test", "value": 42})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0]) == {"event": "test", "value": 42}

    def test_multiple_records_as_separate_lines(self, tmp_path):
        path = tmp_path / "events.jsonl"
        writer = JsonlEventWriter(lambda: path)
        writer.append({"n": 1})
        writer.append({"n": 2})
        writer.append({"n": 3})
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        assert [json.loads(l)["n"] for l in lines] == [1, 2, 3]

    def test_file_ends_with_newline(self, tmp_path):
        path = tmp_path / "events.jsonl"
        writer = JsonlEventWriter(lambda: path)
        writer.append({"x": 1})
        assert path.read_text(encoding="utf-8").endswith("\n")

    def test_creates_parent_dirs_if_missing(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "events.jsonl"
        writer = JsonlEventWriter(lambda: path)
        writer.append({"ok": True})
        assert path.exists()

    def test_sanitize_true_coerces_datetime(self, tmp_path):
        path = tmp_path / "events.jsonl"
        writer = JsonlEventWriter(lambda: path, sanitize=True)
        dt = datetime.datetime(2026, 5, 18, 0, 0, 0, tzinfo=datetime.timezone.utc)
        writer.append({"ts": dt})
        line = path.read_text(encoding="utf-8").strip()
        parsed = json.loads(line)
        assert isinstance(parsed["ts"], str)
        assert "2026-05-18" in parsed["ts"]

    def test_sanitize_false_raises_on_non_serializable(self, tmp_path):
        path = tmp_path / "events.jsonl"
        writer = JsonlEventWriter(lambda: path, sanitize=False)
        dt = datetime.datetime(2026, 5, 18)
        with pytest.raises(TypeError):
            writer.append({"ts": dt})

    def test_path_factory_called_on_each_append(self, tmp_path):
        calls = []
        paths = [tmp_path / "a.jsonl", tmp_path / "b.jsonl"]

        def rotating_factory():
            i = len(calls)
            calls.append(i)
            return paths[i % 2]

        writer = JsonlEventWriter(rotating_factory)
        writer.append({"n": 0})
        writer.append({"n": 1})
        assert len(calls) == 2

    def test_ensure_ascii_false_preserves_unicode(self, tmp_path):
        path = tmp_path / "events.jsonl"
        writer = JsonlEventWriter(lambda: path, ensure_ascii=False)
        writer.append({"text": "こんにちは"})
        raw = path.read_bytes()
        assert "こんにちは".encode("utf-8") in raw

    def test_ensure_ascii_true_escapes_unicode(self, tmp_path):
        path = tmp_path / "events.jsonl"
        writer = JsonlEventWriter(lambda: path, ensure_ascii=True)
        writer.append({"text": "こんにちは"})
        line = path.read_text(encoding="utf-8").strip()
        assert "\\u" in line
