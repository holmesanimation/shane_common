"""Tests for shane_common.watchdog.audit.AppendOnlyAuditLog."""
from __future__ import annotations

import json
from pathlib import Path

from shane_common.watchdog.audit import AppendOnlyAuditLog


class TestAppendOnlyAuditLog:
    def test_append_creates_file(self, tmp_path: Path) -> None:
        log = AppendOnlyAuditLog(tmp_path / "audit" / "test.audit.jsonl")
        log.append({"event": "test", "value": 1})
        assert (tmp_path / "audit" / "test.audit.jsonl").exists()

    def test_append_writes_valid_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "test.audit.jsonl"
        log = AppendOnlyAuditLog(path)
        log.append({"event": "first"})
        log.append({"event": "second"})
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "first"
        assert json.loads(lines[1])["event"] == "second"

    def test_tail_returns_last_n(self, tmp_path: Path) -> None:
        path = tmp_path / "test.audit.jsonl"
        log = AppendOnlyAuditLog(path)
        for i in range(10):
            log.append({"seq": i})
        records = log.tail(3)
        assert len(records) == 3
        assert records[-1]["seq"] == 9

    def test_tail_on_missing_file_returns_empty(self, tmp_path: Path) -> None:
        log = AppendOnlyAuditLog(tmp_path / "nonexistent.jsonl")
        assert log.tail(10) == []

    def test_append_non_ascii(self, tmp_path: Path) -> None:
        path = tmp_path / "unicode.audit.jsonl"
        log = AppendOnlyAuditLog(path)
        log.append({"note": "supervisor_tray_quit: opérateur"})
        text = path.read_text(encoding="utf-8")
        assert "opérateur" in text

    def test_multiple_append_grows_file(self, tmp_path: Path) -> None:
        path = tmp_path / "grow.audit.jsonl"
        log = AppendOnlyAuditLog(path)
        for i in range(5):
            log.append({"i": i})
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 5
