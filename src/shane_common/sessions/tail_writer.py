"""Session tail writer — lightweight crash-resilient run-tail snapshot."""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from shane_common.io.atomic import write_json_atomic
from shane_common.time import local_now_iso


@dataclass
class SessionTailState:
    last_clock_ts: Optional[float] = None
    last_event_ts: Optional[float] = None
    last_by_stream: Dict[str, float] = field(default_factory=dict)


class SessionTailWriter:
    """
    Persist a lightweight per-run tail snapshot for crash-resilient end-time inference.

    IMPORTANT: Never calls JournalService.emit() — standalone side-channel only.
    """

    def __init__(self, system_root: str, run_id: str) -> None:
        self.run_id = str(run_id)
        self.state = SessionTailState()
        self.path = (
            Path(system_root) / "sessions" / run_id / f"session_tail.{run_id}.json"
        )

    def note_clock(self, ts: float) -> None:
        """Update last_clock_ts if ts is newer."""
        try:
            t = float(ts)
            prev = self.state.last_clock_ts
            if prev is None or t > prev:
                self.state.last_clock_ts = t
                self._persist_best_effort()
        except Exception:
            traceback.print_exc()

    def note_event(self, ts: float, stream: str) -> None:
        """Update last_event_ts and last_by_stream[stream] if ts is newer for that stream."""
        try:
            t = float(ts)
            s = str(stream or "").strip() or "unknown"

            prev_stream = self.state.last_by_stream.get(s)
            if prev_stream is None or t > prev_stream:
                self.state.last_by_stream[s] = t

            prev_event = self.state.last_event_ts
            if prev_event is None or t > prev_event:
                self.state.last_event_ts = t

            self._persist_best_effort()
        except Exception:
            traceback.print_exc()

    def flush(self, reason: str = "flush") -> None:
        """Force a persist (best-effort). Does not emit journal events."""
        _ = reason
        self._persist_best_effort()

    def _persist_best_effort(self) -> None:
        try:
            payload = {
                "local_TS": local_now_iso(),
                "run_id": self.run_id,
                "last_clock_ts": self.state.last_clock_ts,
                "last_event_ts": self.state.last_event_ts,
                "last_by_stream": dict(self.state.last_by_stream),
                "write_ts": time.time(),
            }
            write_json_atomic(self.path, payload, sort_keys=False)
        except Exception:
            traceback.print_exc()
