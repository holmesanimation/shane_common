"""Tests for shane_common.notify adapters: Null, Bark, Composite, Telegram."""

from __future__ import annotations

import json
from typing import List
from unittest.mock import MagicMock, patch

import pytest

from shane_common.notify.contracts import NotificationEvent, NotificationSeverity
from shane_common.notify.adapters.null_adapter import NullNotifyAdapter
from shane_common.notify.adapters.composite_adapter import CompositeNotifyAdapter
from shane_common.notify.adapters.bark_adapter import BarkConfig, BarkNotifyAdapter
from shane_common.notify.adapters.telegram_adapter import TelegramNotifyAdapter


def _evt(
    *,
    kind: str = "test.event",
    scope: str = "global",
    severity: NotificationSeverity = NotificationSeverity.INFO,
    ts: float = 1000.0,
    details: str | None = None,
    data: dict | None = None,
) -> NotificationEvent:
    return NotificationEvent(
        ts=ts,
        severity=severity,
        kind=kind,
        scope=scope,
        details=details,
        data=data,
    )


# ---------------------------------------------------------------------------
# NullNotifyAdapter
# ---------------------------------------------------------------------------

def test_null_adapter_never_raises():
    adapter = NullNotifyAdapter()
    adapter.send(_evt())  # must not raise


# ---------------------------------------------------------------------------
# CompositeNotifyAdapter
# ---------------------------------------------------------------------------

def test_composite_delivers_to_all():
    calls: List[str] = []

    class _A:
        def __init__(self, name: str) -> None:
            self._name = name

        def send(self, event: NotificationEvent) -> None:
            calls.append(self._name)

    composite = CompositeNotifyAdapter([_A("first"), _A("second")])
    composite.send(_evt())

    assert calls == ["first", "second"]


def test_composite_continues_after_one_failure():
    calls: List[str] = []

    class _Failing:
        def send(self, event: NotificationEvent) -> None:
            raise RuntimeError("boom")

    class _Good:
        def send(self, event: NotificationEvent) -> None:
            calls.append("good")

    composite = CompositeNotifyAdapter([_Failing(), _Good()])
    composite.send(_evt())  # must not raise

    assert calls == ["good"]


# ---------------------------------------------------------------------------
# BarkNotifyAdapter
# ---------------------------------------------------------------------------

def test_bark_title_format():
    adapter = BarkNotifyAdapter(BarkConfig(device_key="k", group="g"))
    title, body = adapter._format(_evt(kind="strategy.error", severity=NotificationSeverity.URGENT))
    assert title == "[URGENT] strategy.error"
    assert "scope=global" in body


def test_bark_sound_only_for_warning_urgent():
    cfg = BarkConfig(device_key="k", group="g", sound="alarm")
    adapter = BarkNotifyAdapter(cfg)

    # Simulate what _format produces; check sound param logic
    info_evt = _evt(severity=NotificationSeverity.INFO)
    # Sound should NOT be added for INFO — test via the send() URL assembly path
    with patch("urllib.request.urlopen") as mock_open:
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b""
        mock_open.return_value = mock_resp

        adapter.send(info_evt)
        req = mock_open.call_args[0][0]
        assert "sound" not in req.full_url


def test_bark_does_not_raise_on_network_error():
    adapter = BarkNotifyAdapter(BarkConfig(device_key="k", group="g"))
    with patch("urllib.request.urlopen", side_effect=OSError("network down")):
        adapter.send(_evt())  # must not raise


def test_bark_delivers_all_event_kinds():
    """BarkNotifyAdapter has no kind-based filter — all events are forwarded."""
    adapter = BarkNotifyAdapter(BarkConfig(device_key="k", group="g"))
    stale_evt = _evt(kind="marketdata.stale_or_missing")
    with patch("urllib.request.urlopen") as mock_open:
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b""
        mock_open.return_value = mock_resp
        adapter.send(stale_evt)
        mock_open.assert_called_once()


# ---------------------------------------------------------------------------
# TelegramNotifyAdapter
# ---------------------------------------------------------------------------

def test_telegram_skips_severity_not_in_set():
    calls: List[NotificationEvent] = []

    class _Capture:
        def __init__(self, outer):
            self._outer = outer

        def send(self, event):
            calls.append(event)

    # Build adapter that only delivers WARNING/URGENT
    adapter = TelegramNotifyAdapter(
        bot_token="tok",
        recipient_ids=[123],
        alert_severities=frozenset({"WARNING", "URGENT"}),
    )
    with patch("urllib.request.urlopen") as mock_open:
        adapter.send(_evt(severity=NotificationSeverity.INFO))
        mock_open.assert_not_called()


def test_telegram_delivers_matching_severity():
    with patch("urllib.request.urlopen") as mock_open:
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b""
        mock_open.return_value = mock_resp

        adapter = TelegramNotifyAdapter(
            bot_token="tok",
            recipient_ids=[123, 456],
            alert_severities=frozenset({"WARNING", "URGENT"}),
        )
        adapter.send(_evt(severity=NotificationSeverity.URGENT))

        # Called once per recipient
        assert mock_open.call_count == 2


def test_telegram_custom_formatter_used():
    formatted: List[str] = []

    def my_formatter(event: NotificationEvent) -> str:
        msg = f"CUSTOM: {event.kind}"
        formatted.append(msg)
        return msg

    with patch("urllib.request.urlopen") as mock_open:
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = b""
        mock_open.return_value = mock_resp

        adapter = TelegramNotifyAdapter(
            bot_token="tok",
            recipient_ids=[1],
            alert_severities=frozenset({"URGENT"}),
            format_message=my_formatter,
        )
        adapter.send(_evt(severity=NotificationSeverity.URGENT, kind="my.event"))

    assert formatted == ["CUSTOM: my.event"]


def test_telegram_reply_markup_injected():
    def my_markup(event: NotificationEvent) -> dict | None:
        return {"inline_keyboard": [[{"text": "OK", "callback_data": "ok"}]]}

    sent_bodies: List[bytes] = []

    class _FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b""

    def fake_urlopen(req, timeout=None):
        sent_bodies.append(req.data)
        return _FakeResp()

    adapter = TelegramNotifyAdapter(
        bot_token="tok",
        recipient_ids=[1],
        alert_severities=frozenset({"URGENT"}),
        build_reply_markup=my_markup,
    )
    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        adapter.send(_evt(severity=NotificationSeverity.URGENT))

    assert sent_bodies
    body = sent_bodies[0].decode("utf-8")
    assert "reply_markup" in body


def test_telegram_does_not_raise_on_formatter_error():
    def bad_formatter(event: NotificationEvent) -> str:
        raise RuntimeError("formatter crashed")

    adapter = TelegramNotifyAdapter(
        bot_token="tok",
        recipient_ids=[1],
        alert_severities=frozenset({"URGENT"}),
        format_message=bad_formatter,
    )
    with patch("urllib.request.urlopen"):
        adapter.send(_evt(severity=NotificationSeverity.URGENT))  # must not raise


def test_telegram_does_not_raise_on_network_error():
    adapter = TelegramNotifyAdapter(
        bot_token="tok",
        recipient_ids=[1],
        alert_severities=frozenset({"URGENT"}),
    )
    with patch("urllib.request.urlopen", side_effect=OSError("network down")):
        adapter.send(_evt(severity=NotificationSeverity.URGENT))  # must not raise
