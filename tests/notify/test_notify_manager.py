"""Tests for shane_common.notify.notify_manager."""

from __future__ import annotations

import time
from typing import List

import pytest

from shane_common.notify.contracts import NotificationEvent, NotificationSeverity
from shane_common.notify.notify_manager import NotifyManager
from shane_common.notify.adapters.null_adapter import NullNotifyAdapter


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class _CapturingAdapter:
    """Records every event sent to it; never raises."""

    def __init__(self) -> None:
        self.sent: List[NotificationEvent] = []

    def send(self, event: NotificationEvent) -> None:
        self.sent.append(event)


class _RaisingAdapter:
    """Always raises; used to test that NotifyManager catches adapter failures."""

    def send(self, event: NotificationEvent) -> None:
        raise RuntimeError("adapter exploded")


def _make_event(
    *,
    kind: str = "test.event",
    scope: str = "global",
    severity: NotificationSeverity = NotificationSeverity.INFO,
    ts: float = 1000.0,
    dedupe_key: str | None = None,
) -> NotificationEvent:
    return NotificationEvent(
        ts=ts,
        severity=severity,
        kind=kind,
        scope=scope,
        dedupe_key=dedupe_key,
    )


def _make_manager(
    adapter=None,
    *,
    min_default: float = 0.0,
    min_warning: float = 0.0,
    min_urgent: float = 0.0,
    enabled: bool = True,
    clock_fn=None,
) -> NotifyManager:
    return NotifyManager(
        adapter=adapter or _CapturingAdapter(),
        clock_fn=clock_fn,
        min_emit_interval_s_default=min_default,
        min_emit_interval_s_warning=min_warning,
        min_emit_interval_s_urgent=min_urgent,
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# Tests: enabled / disabled
# ---------------------------------------------------------------------------

def test_disabled_suppresses_all():
    adapter = _CapturingAdapter()
    mgr = _make_manager(adapter, enabled=False)
    result = mgr.emit(_make_event())
    assert result is None
    assert adapter.sent == []


def test_enabled_delivers():
    adapter = _CapturingAdapter()
    mgr = _make_manager(adapter)
    result = mgr.emit(_make_event(ts=1000.0))
    assert result is not None
    assert len(adapter.sent) == 1


# ---------------------------------------------------------------------------
# Tests: idempotency (same event_id not re-delivered)
# ---------------------------------------------------------------------------

def test_same_event_id_not_re_delivered():
    """Same logical event (same kind/scope/severity/dedupe_key) is not re-delivered."""
    adapter = _CapturingAdapter()
    mgr = _make_manager(adapter)

    evt1 = _make_event(ts=1000.0, dedupe_key="x")
    evt2 = _make_event(ts=2000.0, dedupe_key="x")  # same hash, different ts

    mgr.emit(evt1)
    mgr.emit(evt2)

    # ts is not included in the hash, so both compute to the same event_id
    assert len(adapter.sent) == 1


def test_different_dedupe_key_delivers_separately():
    adapter = _CapturingAdapter()
    mgr = _make_manager(adapter)

    mgr.emit(_make_event(ts=1000.0, dedupe_key="a"))
    mgr.emit(_make_event(ts=1000.0, dedupe_key="b"))

    assert len(adapter.sent) == 2


def test_different_kind_delivers_separately():
    adapter = _CapturingAdapter()
    mgr = _make_manager(adapter)

    mgr.emit(_make_event(ts=1000.0, kind="foo.error"))
    mgr.emit(_make_event(ts=1000.0, kind="bar.error"))

    assert len(adapter.sent) == 2


# ---------------------------------------------------------------------------
# Tests: anti-spam windowing
# ---------------------------------------------------------------------------

def test_anti_spam_suppresses_same_event_twice():
    """Same event_id (same kind/scope/severity/dedupe_key) is blocked on the second call."""
    adapter = _CapturingAdapter()
    mgr = _make_manager(adapter, min_default=60.0)

    r1 = mgr.emit(_make_event(ts=1000.0, kind="k1", dedupe_key="k1"))
    r2 = mgr.emit(_make_event(ts=1010.0, kind="k1", dedupe_key="k1"))  # same event_id

    assert r1 is not None
    assert r2 is None
    assert len(adapter.sent) == 1


def test_anti_spam_allows_after_interval():
    adapter = _CapturingAdapter()
    mgr = _make_manager(adapter, min_default=60.0)

    r1 = mgr.emit(_make_event(ts=1000.0, kind="k2", dedupe_key="a"))
    r2 = mgr.emit(_make_event(ts=2000.0, kind="k2", dedupe_key="b"))  # 1000 s later

    assert r1 is not None
    assert r2 is not None
    assert len(adapter.sent) == 2


def test_different_severities_independent_event_ids():
    """INFO and URGENT events for the same kind/scope/dedupe_key have different event_ids."""
    adapter = _CapturingAdapter()
    mgr = _make_manager(adapter)

    r1 = mgr.emit(_make_event(ts=1000.0, kind="s", severity=NotificationSeverity.INFO, dedupe_key="d"))
    r2 = mgr.emit(_make_event(ts=1000.0, kind="s", severity=NotificationSeverity.URGENT, dedupe_key="d"))

    # Different severity → different event_id → both delivered
    assert r1 is not None
    assert r2 is not None
    assert len(adapter.sent) == 2


# ---------------------------------------------------------------------------
# Tests: event_id stability
# ---------------------------------------------------------------------------

def test_event_id_excludes_ts():
    """Changing ts must NOT change event_id."""
    adapter = _CapturingAdapter()
    mgr = _make_manager(adapter)

    evt1 = _make_event(ts=100.0, kind="x", scope="global", dedupe_key="z")
    evt2 = _make_event(ts=999.0, kind="x", scope="global", dedupe_key="z")

    id1 = mgr._event_id(evt1)
    id2 = mgr._event_id(evt2)
    assert id1 == id2


def test_event_id_differs_across_kinds():
    mgr = _make_manager()
    id1 = mgr._event_id(_make_event(kind="foo"))
    id2 = mgr._event_id(_make_event(kind="bar"))
    assert id1 != id2


# ---------------------------------------------------------------------------
# Tests: adapter error isolation
# ---------------------------------------------------------------------------

def test_raising_adapter_does_not_propagate():
    mgr = _make_manager(_RaisingAdapter())
    # Must not raise
    result = mgr.emit(_make_event(ts=1000.0))
    assert result is None


# ---------------------------------------------------------------------------
# Tests: clock_fn
# ---------------------------------------------------------------------------

def test_clock_fn_used_when_ts_not_provided():
    adapter = _CapturingAdapter()
    fixed_ts = [5000.0]
    mgr = NotifyManager(
        adapter=adapter,
        clock_fn=lambda: fixed_ts[0],
    )

    mgr.notify_info(kind="test", scope="app", dedupe_key="clock-test")
    assert len(adapter.sent) == 1
    assert adapter.sent[0].ts == 5000.0


def test_clock_fn_none_falls_back_to_wall_clock():
    adapter = _CapturingAdapter()
    before = time.time()
    mgr = NotifyManager(adapter=adapter)
    mgr.notify_info(kind="test", scope="app", dedupe_key="wall-test")
    after = time.time()

    assert len(adapter.sent) == 1
    assert before <= adapter.sent[0].ts <= after


# ---------------------------------------------------------------------------
# Tests: helper wrappers (notify_info / notify_warning / notify_urgent)
# ---------------------------------------------------------------------------

def test_notify_info_severity():
    adapter = _CapturingAdapter()
    mgr = _make_manager(adapter)
    mgr.notify_info(kind="t", scope="s", ts=1.0, dedupe_key="i")
    assert adapter.sent[0].severity == NotificationSeverity.INFO


def test_notify_warning_severity():
    adapter = _CapturingAdapter()
    mgr = _make_manager(adapter)
    mgr.notify_warning(kind="t", scope="s", ts=1.0, dedupe_key="w")
    assert adapter.sent[0].severity == NotificationSeverity.WARNING


def test_notify_urgent_severity():
    adapter = _CapturingAdapter()
    mgr = _make_manager(adapter)
    mgr.notify_urgent(kind="t", scope="s", ts=1.0, dedupe_key="u")
    assert adapter.sent[0].severity == NotificationSeverity.URGENT
