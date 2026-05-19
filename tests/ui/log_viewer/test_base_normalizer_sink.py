# shane_common/tests/ui/log_viewer/test_base_normalizer_sink.py
"""
Tests for BaseLogNormalizerSink.

Uses CallbackLogRowEmitter — no Qt process required.
"""
from __future__ import annotations

import time
from typing import List

import pytest

from shane_common.ui.log_viewer.base_normalizer_sink import BaseLogNormalizerSink
from shane_common.ui.log_viewer.kind_spec import KindSpec, _DEFAULT_KIND_SPEC
from shane_common.ui.log_viewer.log_row import LogRow
from shane_common.ui.log_viewer.log_row_emitter import CallbackLogRowEmitter


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #

def _make_sink(kind_map: dict | None = None):
    """Build a concrete sink backed by a CallbackLogRowEmitter."""
    rows: List[LogRow] = []

    def kind_spec_fn(kind):
        if kind_map is None:
            return None
        return kind_map.get(kind)

    emitter = CallbackLogRowEmitter(on_row=rows.append)

    class _TestSink(BaseLogNormalizerSink):
        pass

    sink = _TestSink(kind_spec_fn=kind_spec_fn, emitter=emitter)
    return sink, rows


def _envelope(kind: str, ts: float = 1_700_000_000.0, instrument: str | None = None, **payload) -> dict:
    env: dict = {"kind": kind, "ts": ts}
    if instrument is not None:
        env["instrument"] = instrument
    if payload:
        env["payload"] = payload
    return env


# ------------------------------------------------------------------ #
# Tests
# ------------------------------------------------------------------ #

class TestEmitBasic:
    def test_valid_envelope_produces_log_row(self):
        kind_map = {"sys.start": KindSpec(severity="INFO", message_template="Started")}
        sink, rows = _make_sink(kind_map)
        sink.emit(_envelope("sys.start"))
        assert len(rows) == 1
        assert rows[0].kind == "sys.start"
        assert rows[0].severity == "INFO"
        assert rows[0].message == "Started"

    def test_unknown_kind_uses_default_spec(self):
        sink, rows = _make_sink({})
        sink.emit(_envelope("unknown.kind"))
        assert len(rows) == 1
        assert rows[0].severity == _DEFAULT_KIND_SPEC.severity
        assert rows[0].importance == _DEFAULT_KIND_SPEC.importance

    def test_emit_never_raises_on_malformed_input(self):
        sink, rows = _make_sink()
        # Various malformed inputs
        sink.emit({})
        sink.emit({"kind": ""})
        sink.emit({"kind": "x"})              # missing ts
        sink.emit({"kind": "x", "ts": None})  # null ts
        sink.emit({"kind": "x", "ts": "not-a-date"})  # bad ts
        # None of the above should raise

    def test_instrument_none_for_underscore(self):
        kind_map = {"ev": KindSpec(severity="DEBUG", message_template="ev")}
        sink, rows = _make_sink(kind_map)
        env = _envelope("ev")
        env["instrument"] = "_"
        sink.emit(env)
        assert rows[0].instrument is None

    def test_instrument_populated(self):
        kind_map = {"ev": KindSpec(severity="DEBUG", message_template="ev")}
        sink, rows = _make_sink(kind_map)
        sink.emit(_envelope("ev", instrument="TSLA"))
        assert rows[0].instrument == "TSLA"

    def test_iso_ts_string_parsed(self):
        kind_map = {"ev": KindSpec(severity="DEBUG", message_template="ev")}
        sink, rows = _make_sink(kind_map)
        sink.emit({"kind": "ev", "ts": "2023-11-14T10:00:00+00:00"})
        assert abs(rows[0].ts - 1_699_956_000.0) < 2.0

    def test_severity_fn_overrides(self):
        kind_map = {
            "feed": KindSpec(
                severity="WARN",
                message_template="Feed {state}",
                severity_fn=lambda p: "INFO" if p.get("state") == "healthy" else "WARN",
            )
        }
        sink, rows = _make_sink(kind_map)
        sink.emit(_envelope("feed", state="healthy"))
        assert rows[0].severity == "INFO"

    def test_message_fn_overrides(self):
        kind_map = {
            "gate": KindSpec(
                severity="WARN",
                message_template="default",
                message_fn=lambda p: f"Gate: {p.get('gate')}",
            )
        }
        sink, rows = _make_sink(kind_map)
        sink.emit(_envelope("gate", gate="no_plan"))
        assert rows[0].message == "Gate: no_plan"

    def test_problem_fn_overrides(self):
        kind_map = {
            "ev": KindSpec(
                severity="DEBUG",
                message_template="ev",
                problem_fn=lambda p: not p.get("ok", True),
            )
        }
        sink, rows = _make_sink(kind_map)
        sink.emit(_envelope("ev", ok=False))
        assert rows[0].problem is True

    def test_details_includes_payload_and_correlation(self):
        kind_map = {"ev": KindSpec(severity="DEBUG", message_template="ev")}
        sink, rows = _make_sink(kind_map)
        env = _envelope("ev", foo="bar")
        env["correlation"] = {"run_id": "abc"}
        sink.emit(env)
        assert rows[0].details.get("foo") == "bar"
        assert rows[0].details.get("run_id") == "abc"


class TestDedupeHard:
    def test_dedupe_suppresses_within_interval(self):
        kind_map = {
            "alive": KindSpec(
                severity="DEBUG",
                message_template="alive",
                dedupe_interval_s=60.0,
            )
        }
        sink, rows = _make_sink(kind_map)
        ts = 1_700_000_000.0
        sink.emit(_envelope("alive", ts=ts))
        sink.emit(_envelope("alive", ts=ts + 5.0))   # within 60s — suppressed
        sink.emit(_envelope("alive", ts=ts + 10.0))  # within 60s — suppressed
        assert len(rows) == 1

    def test_dedupe_emits_after_interval(self):
        kind_map = {
            "alive": KindSpec(
                severity="DEBUG",
                message_template="alive",
                dedupe_interval_s=60.0,
            )
        }
        sink, rows = _make_sink(kind_map)
        ts = 1_700_000_000.0
        sink.emit(_envelope("alive", ts=ts))
        sink.emit(_envelope("alive", ts=ts + 61.0))  # interval elapsed
        assert len(rows) == 2

    def test_dedupe_carries_count(self):
        kind_map = {
            "alive": KindSpec(
                severity="DEBUG",
                message_template="alive",
                dedupe_interval_s=60.0,
            )
        }
        sink, rows = _make_sink(kind_map)
        ts = 1_700_000_000.0
        sink.emit(_envelope("alive", ts=ts))
        sink.emit(_envelope("alive", ts=ts + 5.0))
        sink.emit(_envelope("alive", ts=ts + 10.0))
        sink.emit(_envelope("alive", ts=ts + 61.0))  # emits with count=2
        assert rows[1].dedupe_count == 2


class TestDedupeUntilChanged:
    def test_consecutive_identical_suppressed(self):
        kind_map = {
            "snap": KindSpec(
                severity="DEBUG",
                message_template="Snap {x}",
                dedupe_until_changed=True,
            )
        }
        sink, rows = _make_sink(kind_map)
        ts = 1_700_000_000.0
        sink.emit(_envelope("snap", ts=ts, x=1))
        sink.emit(_envelope("snap", ts=ts + 1.0, x=1))  # same message — suppressed
        assert len(rows) == 1

    def test_changed_message_emits(self):
        kind_map = {
            "snap": KindSpec(
                severity="DEBUG",
                message_template="Snap {x}",
                dedupe_until_changed=True,
            )
        }
        sink, rows = _make_sink(kind_map)
        ts = 1_700_000_000.0
        sink.emit(_envelope("snap", ts=ts, x=1))
        sink.emit(_envelope("snap", ts=ts + 1.0, x=2))  # message changed — emits
        assert len(rows) == 2


class TestDrainBuffer:
    def test_drain_buffer_returns_buffered_rows(self):
        kind_map = {"ev": KindSpec(severity="DEBUG", message_template="ev")}
        sink, rows = _make_sink(kind_map)
        sink.emit(_envelope("ev", ts=1_700_000_000.0))
        sink.emit(_envelope("ev", ts=1_700_000_001.0))
        buffered = sink.drain_buffer()
        assert len(buffered) == 2

    def test_drain_buffer_order(self):
        kind_map = {"ev": KindSpec(severity="DEBUG", message_template="ev")}
        sink, rows = _make_sink(kind_map)
        for i in range(5):
            sink.emit(_envelope("ev", ts=float(1_700_000_000 + i)))
        buffered = sink.drain_buffer()
        ts_list = [r.ts for r in buffered]
        assert ts_list == sorted(ts_list)


class TestPostEmitHook:
    def test_post_emit_called_on_each_row(self):
        kind_map = {"ev": KindSpec(severity="DEBUG", message_template="ev")}
        hook_calls: List[LogRow] = []

        emitter = CallbackLogRowEmitter(on_row=lambda r: None)

        class _HookSink(BaseLogNormalizerSink):
            def _post_emit(self, row: LogRow) -> None:
                hook_calls.append(row)

        sink = _HookSink(kind_spec_fn=kind_map.get, emitter=emitter)
        sink.emit(_envelope("ev", ts=1_700_000_000.0))
        sink.emit(_envelope("ev", ts=1_700_000_001.0))
        assert len(hook_calls) == 2

    def test_post_emit_not_called_when_suppressed(self):
        kind_map = {
            "ev": KindSpec(
                severity="DEBUG",
                message_template="ev",
                dedupe_interval_s=60.0,
            )
        }
        hook_calls: List[LogRow] = []
        emitter = CallbackLogRowEmitter(on_row=lambda r: None)

        class _HookSink(BaseLogNormalizerSink):
            def _post_emit(self, row: LogRow) -> None:
                hook_calls.append(row)

        sink = _HookSink(kind_spec_fn=kind_map.get, emitter=emitter)
        ts = 1_700_000_000.0
        sink.emit(_envelope("ev", ts=ts))
        sink.emit(_envelope("ev", ts=ts + 5.0))  # suppressed
        assert len(hook_calls) == 1


class TestReset:
    def test_reset_clears_dedupe_state(self):
        kind_map = {
            "alive": KindSpec(
                severity="DEBUG",
                message_template="alive",
                dedupe_interval_s=3600.0,
            )
        }
        sink, rows = _make_sink(kind_map)
        ts = 1_700_000_000.0
        sink.emit(_envelope("alive", ts=ts))
        sink.emit(_envelope("alive", ts=ts + 10.0))  # suppressed
        assert len(rows) == 1
        sink.reset()
        sink.emit(_envelope("alive", ts=ts + 20.0))  # reset — allowed
        assert len(rows) == 2
