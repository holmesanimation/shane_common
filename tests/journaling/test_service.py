"""Tests for shane_common.journaling.service."""

from pathlib import Path
from typing import Optional

import pytest

from shane_common.journaling.profile import JournalProfile
from shane_common.journaling.service import JournalConfig, JournalContext, JournalService


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


class _StrictProfile(JournalProfile):
    def validate_kind(self, kind: str) -> Optional[str]:
        if kind != "allowed.kind":
            return f"Invalid kind: {kind!r}"
        return None

    def stream_for_kind(self, kind: str) -> str:
        return "allowed"

    def route_event(self, envelope: dict) -> Path:
        raise NotImplementedError


class _SpySink:
    def __init__(self):
        self.received: list[dict] = []

    def emit(self, envelope: dict) -> None:
        self.received.append(dict(envelope))

    def close(self) -> None:
        pass


class _RaisingSink:
    def emit(self, envelope: dict) -> None:
        raise RuntimeError("sink intentionally broken")

    def close(self) -> None:
        pass


def _make_service(tmp_path, sinks=None, profile=None):
    cfg = JournalConfig(app_id="testapp", app_name="TestApp", run_id="run-1")
    p = profile or _FixedPathProfile(tmp_path)
    return JournalService(cfg, p, sinks or [])


class TestJournalServiceEmit:
    def test_emit_valid_kind_routes_to_both_sinks(self, tmp_path):
        sink1 = _SpySink()
        sink2 = _SpySink()
        svc = _make_service(tmp_path, sinks=[sink1, sink2])
        svc.emit("test.event", {"x": 1})
        assert len(sink1.received) == 1
        assert len(sink2.received) == 1

    def test_sink_failure_does_not_crash_caller(self, tmp_path):
        bad_sink = _RaisingSink()
        svc = _make_service(tmp_path, sinks=[bad_sink])
        # Must not raise
        svc.emit("test.event", {"x": 1})

    def test_emit_error_count_increments_on_outer_exception(self, tmp_path):
        strict = _StrictProfile()
        svc = _make_service(tmp_path, sinks=[], profile=strict)
        assert svc.emit_error_count() == 0
        svc.emit("bad.kind", {})  # invalid kind → ValueError → outer except
        assert svc.emit_error_count() == 1

    def test_sink_failure_does_not_increment_error_count(self, tmp_path):
        bad_sink = _RaisingSink()
        svc = _make_service(tmp_path, sinks=[bad_sink])
        svc.emit("test.event", {})
        assert svc.emit_error_count() == 0

    def test_last_emit_ts_set_after_emit(self, tmp_path):
        svc = _make_service(tmp_path, sinks=[_SpySink()])
        assert svc.last_emit_ts() is None
        svc.emit("test.event", {})
        assert svc.last_emit_ts() is not None

    def test_envelope_has_local_ts_first(self, tmp_path):
        sink = _SpySink()
        svc = _make_service(tmp_path, sinks=[sink])
        svc.emit("test.event", {})
        env = sink.received[0]
        assert list(env.keys())[0] == "local_TS"

    def test_close_sinks_does_not_raise(self, tmp_path):
        svc = _make_service(tmp_path, sinks=[_SpySink()])
        svc.close_sinks()


class TestJournalContext:
    def test_bind_and_get(self):
        with JournalContext.scope(component="my_component"):
            JournalContext.bind(user="alice")
            ctx = JournalContext.get()
            assert ctx["user"] == "alice"
            assert ctx["component"] == "my_component"

    def test_scope_restores_on_exit(self):
        before = JournalContext.get()
        with JournalContext.scope(test_key="val"):
            assert JournalContext.get().get("test_key") == "val"
        after = JournalContext.get()
        assert "test_key" not in after
