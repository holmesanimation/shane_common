"""Per-topic lossless ring buffer for a generic WS event bus.

Each lossless topic maintains one ``TopicRingBuffer``. Events are stamped with a
monotonically increasing ``seq`` counter on append. Subscribers reconnecting with
a known ``last_seq`` can request all events since that sequence number without
data loss (as long as they remain within the ring).

Framework/event-shape neutral: any event object supporting
``model_copy(update={"seq": int})`` (e.g. a pydantic model) may be stored.
"""
from __future__ import annotations

import itertools
import threading
from collections import deque
from typing import Any, List, Optional

_seq_counter = itertools.count(1)


class TopicRingBuffer:
    """Bounded ring buffer for a single lossless topic.

    Thread-safe: all public methods are guarded by an internal lock.

    Parameters
    ----------
    maxlen:
        Maximum number of events retained. When the buffer is full the
        oldest event is silently dropped.
    """

    def __init__(self, maxlen: int = 1000) -> None:
        self._maxlen = maxlen
        self._buf: deque[Any] = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def append(self, event: Any) -> Any:
        """Stamp *event* with the next global seq and enqueue it.

        Returns the (mutated) event so callers can chain publish -> ring -> broadcast.
        """
        stamped = event.model_copy(update={"seq": next(_seq_counter)})
        with self._lock:
            self._buf.append(stamped)
        return stamped

    def since(self, seq: int) -> List[Any]:
        """Return all events whose ``seq`` is strictly greater than *seq*.

        If *seq* is 0 (or lower than all buffered seqs), the entire buffer is
        returned. If the requested seq has been evicted (ring overflow), the
        full buffer is returned so the caller gets the best available history.
        """
        with self._lock:
            buf = list(self._buf)
        return [ev for ev in buf if ev.seq > seq]

    def latest(self) -> Optional[Any]:
        """Return the most-recently appended event, or ``None`` if empty."""
        with self._lock:
            return self._buf[-1] if self._buf else None
