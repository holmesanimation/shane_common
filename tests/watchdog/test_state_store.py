"""Tests for shane_common.watchdog.state_store.WatchdogStateStore."""
from __future__ import annotations

import json
from pathlib import Path

from shane_common.watchdog.models import BaseLockState
from shane_common.watchdog.state_store import WatchdogStateStore


class TestWatchdogStateStore:
    def test_load_lock_returns_default_when_absent(self, tmp_path: Path) -> None:
        store = WatchdogStateStore(tmp_path)
        lock = store.load_lock()
        assert lock.locked is False
        assert lock.manual_intervention_required is False
        assert lock.reason is None

    def test_save_and_reload_lock(self, tmp_path: Path) -> None:
        store = WatchdogStateStore(tmp_path)
        lock = BaseLockState(locked=True, reason="test reason")
        store.save_lock(lock)
        reloaded = store.load_lock()
        assert reloaded.locked is True
        assert reloaded.reason == "test reason"

    def test_load_lock_fail_closed_on_corrupt(self, tmp_path: Path) -> None:
        store = WatchdogStateStore(tmp_path)
        lock_path = tmp_path / "locks" / "global.lock.json"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("NOT JSON{{{", encoding="utf-8")
        lock = store.load_lock()
        assert lock.locked is True
        assert lock.manual_intervention_required is True

    def test_append_and_tail_audit(self, tmp_path: Path) -> None:
        store = WatchdogStateStore(tmp_path)
        store.append_audit({"event": "start"})
        store.append_audit({"event": "stop"})
        records = store.tail_audit(10)
        assert len(records) == 2
        assert records[0]["event"] == "start"
        assert records[1]["event"] == "stop"

    def test_tail_audit_empty_when_no_file(self, tmp_path: Path) -> None:
        store = WatchdogStateStore(tmp_path)
        assert store.tail_audit(10) == []

    def test_save_and_load_snapshot(self, tmp_path: Path) -> None:
        store = WatchdogStateStore(tmp_path)
        store.save_snapshot({"app_states": {"my_app": "HEALTHY"}})
        snap = store.load_snapshot()
        assert snap["app_states"]["my_app"] == "HEALTHY"

    def test_load_snapshot_returns_empty_when_absent(self, tmp_path: Path) -> None:
        store = WatchdogStateStore(tmp_path)
        assert store.load_snapshot() == {}

    def test_lock_file_schema_version_written(self, tmp_path: Path) -> None:
        store = WatchdogStateStore(tmp_path)
        store.save_lock(BaseLockState())
        lock_path = tmp_path / "locks" / "global.lock.json"
        data = json.loads(lock_path.read_text())
        assert "schema_version" in data
