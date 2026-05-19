"""Unit tests for shane_common.io.atomic."""

import json
import os
from pathlib import Path
from unittest.mock import patch, call

import pytest

from shane_common.io.atomic import write_json_atomic, write_text_atomic


class TestWriteJsonAtomic:
    def test_creates_file(self, tmp_path):
        out = tmp_path / "out.json"
        write_json_atomic(out, {"key": "value"})
        assert out.exists()

    def test_content_is_valid_json(self, tmp_path):
        out = tmp_path / "out.json"
        write_json_atomic(out, {"a": 1, "b": [1, 2]})
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data == {"a": 1, "b": [1, 2]}

    def test_creates_parent_directories(self, tmp_path):
        out = tmp_path / "deep" / "nested" / "out.json"
        write_json_atomic(out, {})
        assert out.exists()

    def test_sort_keys_true_by_default(self, tmp_path):
        out = tmp_path / "out.json"
        write_json_atomic(out, {"z": 1, "a": 2})
        text = out.read_text(encoding="utf-8")
        assert text.index('"a"') < text.index('"z"')

    def test_sort_keys_false_preserves_order(self, tmp_path):
        out = tmp_path / "out.json"
        data = {"z": 1, "a": 2}
        write_json_atomic(out, data, sort_keys=False)
        loaded = json.loads(out.read_text(encoding="utf-8"))
        assert list(loaded.keys()) == ["z", "a"]

    def test_overwrites_existing_file(self, tmp_path):
        out = tmp_path / "out.json"
        write_json_atomic(out, {"v": 1})
        write_json_atomic(out, {"v": 2})
        assert json.loads(out.read_text())["v"] == 2

    def test_no_tmp_file_left_behind(self, tmp_path):
        out = tmp_path / "out.json"
        write_json_atomic(out, {})
        tmp = out.with_suffix(".tmp")
        assert not tmp.exists()

    def test_retry_succeeds_after_transient_permission_error(self, tmp_path):
        out = tmp_path / "out.json"
        call_count = [0]
        real_replace = os.replace

        def flaky_replace(src, dst):
            call_count[0] += 1
            if call_count[0] == 1:
                raise PermissionError("transient lock")
            real_replace(src, dst)

        with patch("shane_common.io.atomic.os.replace", side_effect=flaky_replace):
            with patch("shane_common.io.atomic.time.sleep"):
                write_json_atomic(out, {"ok": True})

        assert out.exists()
        assert json.loads(out.read_text())["ok"] is True

    def test_raises_after_retries_exhausted(self, tmp_path):
        out = tmp_path / "out.json"

        with patch("shane_common.io.atomic.os.replace", side_effect=PermissionError("locked")):
            with patch("shane_common.io.atomic.time.sleep"):
                with pytest.raises(PermissionError):
                    write_json_atomic(out, {}, retries=3)


class TestWriteTextAtomic:
    def test_creates_file_with_content(self, tmp_path):
        out = tmp_path / "out.txt"
        write_text_atomic(out, "hello world")
        assert out.read_text(encoding="utf-8") == "hello world"

    def test_creates_parent_directories(self, tmp_path):
        out = tmp_path / "sub" / "out.txt"
        write_text_atomic(out, "text")
        assert out.exists()

    def test_overwrites_existing_file(self, tmp_path):
        out = tmp_path / "out.txt"
        write_text_atomic(out, "first")
        write_text_atomic(out, "second")
        assert out.read_text(encoding="utf-8") == "second"

    def test_no_tmp_file_left_behind(self, tmp_path):
        out = tmp_path / "out.txt"
        write_text_atomic(out, "data")
        assert not out.with_suffix(".tmp").exists()
