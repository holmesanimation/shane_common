"""Tests for shane_common.journaling.gate (EventGate)."""

import pytest

from shane_common.journaling.gate import EventGate


class TestEventGateEmitOnChange:
    def test_fires_on_first_call(self):
        gate = EventGate()
        calls = []
        result = gate.emit_on_change(key="k", signature="sig1", emit_fn=lambda: calls.append(1))
        assert result is True
        assert len(calls) == 1

    def test_suppressed_on_same_signature(self):
        gate = EventGate()
        calls = []
        gate.emit_on_change(key="k", signature="sig1", emit_fn=lambda: calls.append(1))
        gate.emit_on_change(key="k", signature="sig1", emit_fn=lambda: calls.append(2))
        assert len(calls) == 1

    def test_fires_on_signature_change(self):
        gate = EventGate()
        calls = []
        gate.emit_on_change(key="k", signature="sig1", emit_fn=lambda: calls.append(1))
        gate.emit_on_change(key="k", signature="sig2", emit_fn=lambda: calls.append(2))
        assert len(calls) == 2

    def test_independent_keys(self):
        gate = EventGate()
        calls = []
        gate.emit_on_change(key="a", signature="s", emit_fn=lambda: calls.append("a"))
        gate.emit_on_change(key="b", signature="s", emit_fn=lambda: calls.append("b"))
        assert len(calls) == 2


class TestEventGateEmitOnce:
    def test_fires_on_first_token(self):
        gate = EventGate()
        calls = []
        result = gate.emit_once(key="k", token="t1", emit_fn=lambda: calls.append(1))
        assert result is True
        assert len(calls) == 1

    def test_suppressed_on_same_token(self):
        gate = EventGate()
        calls = []
        gate.emit_once(key="k", token="t1", emit_fn=lambda: calls.append(1))
        gate.emit_once(key="k", token="t1", emit_fn=lambda: calls.append(2))
        assert len(calls) == 1

    def test_fires_on_token_change(self):
        gate = EventGate()
        calls = []
        gate.emit_once(key="k", token="t1", emit_fn=lambda: calls.append(1))
        gate.emit_once(key="k", token="t2", emit_fn=lambda: calls.append(2))
        assert len(calls) == 2


class TestEventGateThrottle:
    def test_fires_on_first_call(self):
        gate = EventGate()
        calls = []
        result = gate.throttle(key="k", now_ts=100.0, min_interval_s=10.0, emit_fn=lambda: calls.append(1))
        assert result is True
        assert len(calls) == 1

    def test_suppressed_within_interval(self):
        gate = EventGate()
        calls = []
        gate.throttle(key="k", now_ts=100.0, min_interval_s=10.0, emit_fn=lambda: calls.append(1))
        gate.throttle(key="k", now_ts=105.0, min_interval_s=10.0, emit_fn=lambda: calls.append(2))
        assert len(calls) == 1

    def test_fires_after_interval(self):
        gate = EventGate()
        calls = []
        gate.throttle(key="k", now_ts=100.0, min_interval_s=10.0, emit_fn=lambda: calls.append(1))
        gate.throttle(key="k", now_ts=115.0, min_interval_s=10.0, emit_fn=lambda: calls.append(2))
        assert len(calls) == 2

    def test_independent_keys(self):
        gate = EventGate()
        calls = []
        gate.throttle(key="a", now_ts=100.0, min_interval_s=10.0, emit_fn=lambda: calls.append("a"))
        gate.throttle(key="b", now_ts=100.0, min_interval_s=10.0, emit_fn=lambda: calls.append("b"))
        assert len(calls) == 2
