"""Routed JSONL sink for shane_common journaling."""

from __future__ import annotations

import json
import os
import threading
import traceback
from dataclasses import dataclass
from typing import Any

from shane_common.journaling.profile import JournalProfile


@dataclass(frozen=True)
class JsonlJournalSinkConfig:
    profile: JournalProfile
    flush_each: bool = False
    fsync_each: bool = False


class JsonlJournalSink:
    def __init__(self, cfg: JsonlJournalSinkConfig) -> None:
        self.cfg = cfg
        self._lock = threading.Lock()
        self._handles: dict[str, Any] = {}

    def emit(self, envelope: dict) -> None:
        try:
            path = self.cfg.profile.route_event(envelope)
            path_str = str(path)

            # Preserve dict insertion order so local_TS serializes first (Python 3.7+).
            line = json.dumps(envelope, ensure_ascii=False, separators=(",", ":"))

            with self._lock:
                os.makedirs(os.path.dirname(path_str), exist_ok=True)

                fh = self._handles.get(path_str)
                if fh is None or fh.closed:
                    fh = open(path_str, "a", encoding="utf-8", newline="\n")
                    self._handles[path_str] = fh

                fh.write(line + "\n")

                if self.cfg.flush_each:
                    fh.flush()
                if self.cfg.fsync_each:
                    fh.flush()
                    os.fsync(fh.fileno())

        except Exception:
            traceback.print_exc()

    def close(self) -> None:
        with self._lock:
            for fh in self._handles.values():
                try:
                    fh.close()
                except Exception:
                    pass
            self._handles.clear()
