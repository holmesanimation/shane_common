import threading
import time
from collections import deque
from typing import Any, Dict, List

from shane_common.ipc.contracts.diagnostics import TrafficRecord


class TrafficRecorder:
    def __init__(self, maxlen: int = 500) -> None:
        self._records: deque = deque(maxlen=maxlen)
        self._lock = threading.Lock()

    def record(self, direction: str, envelope: Dict[str, Any]) -> None:
        payload = envelope.get("payload", {}) or {}
        record: TrafficRecord = {
            "direction": direction,
            "ts": envelope.get("ts", time.time()),
            "topic": envelope.get("topic", ""),
            "kind": envelope.get("kind", envelope.get("op", "")),
            "seq": envelope.get("seq", -1),
            "payload_preview": dict(list(payload.items())[:5]) if isinstance(payload, dict) else {},
        }
        with self._lock:
            self._records.append(record)

    def get_recent(self, n: int = 200) -> List[TrafficRecord]:
        with self._lock:
            records = list(self._records)
        return records[-n:]

    def clear(self) -> None:
        with self._lock:
            self._records.clear()
