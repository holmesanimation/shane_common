"""Framework-neutral WebSocket event-bus primitives.

Extracted from `trading_platform/api/server/{ringbuffer,coalesce,subscriptions,ws}.py`
(M2.1 of the Independent Broker Service plan). These classes have no knowledge of
Flask, flask-sock, asyncio, or any specific event payload shape/topic naming — callers
supply their own event objects (anything with an optional `model_copy(update=...)`
method, else falls back to plain attribute copy) and topic/business-logic constants.
"""
from __future__ import annotations

from shane_common.ipc.ws_bus.coalesce import TopicCoalescer
from shane_common.ipc.ws_bus.publisher import WsPublisher
from shane_common.ipc.ws_bus.ringbuffer import TopicRingBuffer
from shane_common.ipc.ws_bus.subscriptions import ConnectionRegistry

__all__ = [
    "TopicCoalescer",
    "WsPublisher",
    "TopicRingBuffer",
    "ConnectionRegistry",
]
