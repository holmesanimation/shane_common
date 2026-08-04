"""Unit tests for shane_common.ipc.ws_bus (generic WS event-bus primitives)."""
from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from shane_common.ipc.ws_bus.coalesce import TopicCoalescer
from shane_common.ipc.ws_bus.publisher import WsPublisher
from shane_common.ipc.ws_bus.ringbuffer import TopicRingBuffer
from shane_common.ipc.ws_bus.subscriptions import ConnectionRegistry


@dataclass(frozen=True)
class _FakeEvent:
    kind: str
    seq: int = 0

    def model_copy(self, update: dict) -> "_FakeEvent":
        return replace(self, **update)


class _FakeWs:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def close(self) -> None:
        self.closed = True


def test_ring_buffer_since_returns_events_after_seq() -> None:
    ring = TopicRingBuffer(maxlen=10)
    for i in range(5):
        ring.append(_FakeEvent(kind=f"e{i}"))

    assert len(ring.since(0)) == 5
    assert len(ring.since(3)) == 2  # seqs 4, 5 (1-indexed global counter)


def test_ring_buffer_latest_returns_last_appended() -> None:
    ring = TopicRingBuffer(maxlen=10)
    ring.append(_FakeEvent(kind="first"))
    stamped_last = ring.append(_FakeEvent(kind="last"))

    assert ring.latest() == stamped_last


def test_coalescer_keeps_only_latest_per_key() -> None:
    coalescer = TopicCoalescer()
    ev1 = _FakeEvent(kind="ev1")
    ev2 = _FakeEvent(kind="ev2")

    coalescer.update("k", ev1)
    coalescer.update("k", ev2)

    assert coalescer.flush() == [ev2]
    assert coalescer.flush() == []  # drained


def test_connection_registry_unfiltered_topic_subscribe_and_lookup() -> None:
    registry = ConnectionRegistry()
    registry.register("conn-1")
    registry.subscribe("conn-1", "bars")

    assert registry.subscribers("bars") == ["conn-1"]


def test_connection_registry_filter_required_topic_raises_without_filters() -> None:
    registry = ConnectionRegistry(filter_required_topics=frozenset({"score"}))
    registry.register("conn-1")

    with pytest.raises(ValueError):
        registry.subscribe("conn-1", "score")


def test_connection_registry_unregister_drops_subscriptions() -> None:
    registry = ConnectionRegistry()
    registry.register("conn-1")
    registry.subscribe("conn-1", "bars")
    registry.unregister("conn-1")

    assert registry.subscribers("bars") == []


def test_ws_publisher_broadcast_sends_to_subscribed_connections() -> None:
    registry = ConnectionRegistry()
    publisher = WsPublisher()
    ws = _FakeWs()

    registry.register("conn-1")
    registry.subscribe("conn-1", "bars")
    publisher.register_ws("conn-1", ws)

    publisher.broadcast("bars", {"price": 100}, registry)

    assert ws.sent == ['{"price": 100}']


def test_ws_publisher_disconnect_all_closes_every_socket() -> None:
    publisher = WsPublisher()
    ws1, ws2 = _FakeWs(), _FakeWs()
    publisher.register_ws("conn-1", ws1)
    publisher.register_ws("conn-2", ws2)

    publisher.disconnect_all()

    assert ws1.closed and ws2.closed
