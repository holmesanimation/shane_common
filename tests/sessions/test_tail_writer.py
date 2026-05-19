"""Tests for SessionTailWriter."""

import json

import pytest

from shane_common.sessions.tail_writer import SessionTailWriter


class TestSessionTailWriter:
    def test_note_clock_persists(self, tmp_path):
        w = SessionTailWriter(str(tmp_path), "run-1")
        w.note_clock(1000.0)
        data = json.loads(w.path.read_text())
        assert data["last_clock_ts"] == pytest.approx(1000.0)

    def test_local_ts_is_first_key(self, tmp_path):
        w = SessionTailWriter(str(tmp_path), "run-1")
        w.note_clock(1000.0)
        data = json.loads(w.path.read_text())
        assert list(data.keys())[0] == "local_TS"

    def test_note_clock_older_ts_does_not_decrease(self, tmp_path):
        w = SessionTailWriter(str(tmp_path), "run-1")
        w.note_clock(2000.0)
        w.note_clock(1000.0)
        assert w.state.last_clock_ts == pytest.approx(2000.0)

    def test_flush_succeeds_without_prior_note(self, tmp_path):
        w = SessionTailWriter(str(tmp_path), "run-1")
        w.flush()  # should not raise even with no prior note_clock

    def test_flush_creates_file(self, tmp_path):
        w = SessionTailWriter(str(tmp_path), "run-1")
        w.note_clock(100.0)
        w.flush()
        assert w.path.exists()

    def test_note_event_updates_last_event_ts(self, tmp_path):
        w = SessionTailWriter(str(tmp_path), "run-1")
        w.note_event(500.0, "market")
        assert w.state.last_event_ts == pytest.approx(500.0)

    def test_note_event_updates_last_by_stream(self, tmp_path):
        w = SessionTailWriter(str(tmp_path), "run-1")
        w.note_event(500.0, "market")
        assert w.state.last_by_stream["market"] == pytest.approx(500.0)

    def test_note_event_persists_last_by_stream(self, tmp_path):
        w = SessionTailWriter(str(tmp_path), "run-1")
        w.note_event(500.0, "market")
        data = json.loads(w.path.read_text())
        assert data["last_by_stream"]["market"] == pytest.approx(500.0)

    def test_note_event_persists_last_event_ts(self, tmp_path):
        w = SessionTailWriter(str(tmp_path), "run-1")
        w.note_event(500.0, "market")
        data = json.loads(w.path.read_text())
        assert data["last_event_ts"] == pytest.approx(500.0)

    def test_note_event_older_stream_ts_does_not_decrease(self, tmp_path):
        w = SessionTailWriter(str(tmp_path), "run-1")
        w.note_event(500.0, "market")
        w.note_event(200.0, "market")
        assert w.state.last_by_stream["market"] == pytest.approx(500.0)
