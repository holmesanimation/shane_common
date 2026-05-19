"""Tests for shane_common.journaling.jsonl_sink."""

import json
from pathlib import Path
from typing import Optional

import pytest

from shane_common.journaling.jsonl_sink import JsonlJournalSink, JsonlJournalSinkConfig
from shane_common.journaling.profile import JournalProfile


class _FixedPathProfile(JournalProfile):
    def __init__(self, base: Path):
        self._base = base

    def validate_kind(self, kind: str) -> Optional[str]:
        return None

    def stream_for_kind(self, kind: str) -> str:
        return kind.split(".")[0] if "." in kind else "misc"

    def route_event(self, envelope: dict) -> Path:
        stream = self.stream_for_kind(str(envelope.get("kind", "misc")))
        return self._base / f"{stream}.jsonl"


def _make_envelope(kind="test.event", payload=None, run_id="run-1"):
    from shane_common.time import local_now_iso, utc_now_iso
    return {
        "local_TS": local_now_iso(),
        "ts": utc_now_iso(),
        "run_id": run_id,
        "kind": kind,
        "source": {"app": "testapp", "component": "test"},
        "payload": payload or {},
    }


def _make_sink(tmp_path, flush_each=True):
    profile = _FixedPathProfile(tmp_path)
    cfg = JsonlJournalSinkConfig(profile=profile, flush_each=flush_each)
    return JsonlJournalSink(cfg)


class TestJsonlJournalSink:
    def test_single_emit_creates_file_with_valid_json_line(self, tmp_path):
        sink = _make_sink(tmp_path)
        env = _make_envelope()
        sink.emit(env)
        sink.close()
        output = (tmp_path / "test.jsonl").read_text(encoding="utf-8")
        lines = [l for l in output.splitlines() if l.strip()]
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["kind"] == "test.event"

    def test_local_ts_is_first_key(self, tmp_path):
        sink = _make_sink(tmp_path)
        env = _make_envelope()
        sink.emit(env)
        sink.close()
        line = (tmp_path / "test.jsonl").read_text(encoding="utf-8").strip()
        data = json.loads(line)
        assert list(data.keys())[0] == "local_TS"

    def test_second_emit_appends(self, tmp_path):
        sink = _make_sink(tmp_path)
        sink.emit(_make_envelope())
        sink.emit(_make_envelope())
        sink.close()
        lines = (tmp_path / "test.jsonl").read_text(encoding="utf-8").splitlines()
        non_empty = [l for l in lines if l.strip()]
        assert len(non_empty) == 2

    def test_unicode_preserved(self, tmp_path):
        sink = _make_sink(tmp_path)
        env = _make_envelope(payload={"msg": "héllo wörld 日本語"})
        sink.emit(env)
        sink.close()
        line = (tmp_path / "test.jsonl").read_text(encoding="utf-8").strip()
        data = json.loads(line)
        assert data["payload"]["msg"] == "héllo wörld 日本語"

    def test_close_does_not_raise_on_already_closed_handles(self, tmp_path):
        sink = _make_sink(tmp_path)
        sink.emit(_make_envelope())
        sink.close()
        sink.close()  # second close should not raise

    def test_parent_dir_created_automatically(self, tmp_path):
        nested = tmp_path / "deep" / "nested"
        profile = _FixedPathProfile(nested)
        cfg = JsonlJournalSinkConfig(profile=profile, flush_each=True)
        sink = JsonlJournalSink(cfg)
        sink.emit(_make_envelope())
        sink.close()
        assert (nested / "test.jsonl").exists()

    def test_emit_does_not_raise_when_route_raises(self, tmp_path):
        from shane_common.journaling.profile import DefaultJournalProfile
        cfg = JsonlJournalSinkConfig(profile=DefaultJournalProfile(), flush_each=True)
        sink = JsonlJournalSink(cfg)
        # DefaultJournalProfile.route_event raises NotImplementedError — sink must not raise
        sink.emit(_make_envelope())
        sink.close()
