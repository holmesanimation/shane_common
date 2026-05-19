"""Tests for shane_common.watchdog.heartbeat_writer and heartbeat_reader."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from shane_common.watchdog.heartbeat_writer import HeartbeatWriter
from shane_common.watchdog.heartbeat_reader import HeartbeatReader
from shane_common.watchdog.models import LivenessHeartbeat


# ---------------------------------------------------------------------------
# HeartbeatWriter
# ---------------------------------------------------------------------------

class TestHeartbeatWriter:
    def test_write_once_creates_file(self, tmp_path: Path) -> None:
        w = HeartbeatWriter(app_id="test_app", heartbeats_dir=tmp_path)
        w._write_once()
        path = tmp_path / "test_app.heartbeat.json"
        assert path.exists()

    def test_written_json_has_required_fields(self, tmp_path: Path) -> None:
        w = HeartbeatWriter(app_id="test_app", heartbeats_dir=tmp_path)
        w._write_once()
        data = json.loads((tmp_path / "test_app.heartbeat.json").read_text())
        assert data["app_id"] == "test_app"
        assert data["pid"] == os.getpid()
        assert data["seq"] == 1
        assert "ts_wall_utc" in data
        assert "schema_version" in data

    def test_seq_increments(self, tmp_path: Path) -> None:
        w = HeartbeatWriter(app_id="seq_app", heartbeats_dir=tmp_path)
        for _ in range(3):
            w._write_once()
        data = json.loads((tmp_path / "seq_app.heartbeat.json").read_text())
        assert data["seq"] == 3

    def test_exit_marker_written_on_stop(self, tmp_path: Path) -> None:
        w = HeartbeatWriter(app_id="exit_app", heartbeats_dir=tmp_path)
        w.stop()
        assert (tmp_path / "exit_app.exit_marker.json").exists()

    def test_start_stop_daemon_thread(self, tmp_path: Path) -> None:
        w = HeartbeatWriter(app_id="thread_app", heartbeats_dir=tmp_path, period_s=0.05)
        w.start()
        time.sleep(0.2)
        w.stop()
        data = json.loads((tmp_path / "thread_app.heartbeat.json").read_text())
        assert data["seq"] >= 1


# ---------------------------------------------------------------------------
# HeartbeatReader
# ---------------------------------------------------------------------------

class TestHeartbeatReader:
    def _write_heartbeat(self, heartbeats_dir: Path, app_id: str, seq: int = 1) -> None:
        path = heartbeats_dir / f"{app_id}.heartbeat.json"
        path.write_text(
            json.dumps({
                "schema_version": 1,
                "app_id": app_id,
                "pid": 12345,
                "ts_wall_utc": time.time(),
                "seq": seq,
            }),
            encoding="utf-8",
        )

    def test_read_returns_none_when_absent(self, tmp_path: Path) -> None:
        r = HeartbeatReader(tmp_path)
        hb, mtime, exit_marker = r.read("missing_app")
        assert hb is None
        assert mtime is None
        assert exit_marker is False

    def test_read_returns_heartbeat(self, tmp_path: Path) -> None:
        self._write_heartbeat(tmp_path, "my_app", seq=5)
        r = HeartbeatReader(tmp_path)
        hb, mtime, exit_marker = r.read("my_app")
        assert isinstance(hb, LivenessHeartbeat)
        assert hb.app_id == "my_app"
        assert hb.seq == 5
        assert mtime is not None
        assert exit_marker is False

    def test_exit_marker_detected(self, tmp_path: Path) -> None:
        self._write_heartbeat(tmp_path, "exiting_app")
        (tmp_path / "exiting_app.exit_marker.json").write_text("{}", encoding="utf-8")
        r = HeartbeatReader(tmp_path)
        _, _, exit_marker = r.read("exiting_app")
        assert exit_marker is True

    def test_list_known_app_ids(self, tmp_path: Path) -> None:
        self._write_heartbeat(tmp_path, "app_a")
        self._write_heartbeat(tmp_path, "app_b")
        r = HeartbeatReader(tmp_path)
        ids = sorted(r.list_known_app_ids())
        assert ids == ["app_a", "app_b"]

    def test_is_stale(self, tmp_path: Path) -> None:
        r = HeartbeatReader(tmp_path, stale_s=5.0)
        old_ts = time.time() - 10.0
        assert r.is_stale(old_ts) is True
        fresh_ts = time.time() - 1.0
        assert r.is_stale(fresh_ts) is False

    def test_is_dead(self, tmp_path: Path) -> None:
        r = HeartbeatReader(tmp_path, dead_s=30.0)
        old_ts = time.time() - 60.0
        assert r.is_dead(old_ts) is True
        recent_ts = time.time() - 10.0
        assert r.is_dead(recent_ts) is False
