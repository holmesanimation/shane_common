"""
Generic HeartbeatReader — reads heartbeat and exit-marker files written by
monitored applications.

This module is read-only: it never writes to the heartbeats directory.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from .models import LivenessHeartbeat

log = logging.getLogger(__name__)

_READ_RETRIES: int = 4
_READ_RETRY_DELAY_S: float = 0.03
_SLOW_HEARTBEAT_READ_THRESHOLD_MS: float = 50.0


class HeartbeatReader:
    """
    Reads heartbeat files for all monitored applications.

    Parameters
    ----------
    heartbeats_dir:
        Directory where ``.heartbeat.json`` and ``.exit_marker.json`` files live.
    stale_s:
        Seconds after which a heartbeat is considered STALE.
    dead_s:
        Seconds after which a heartbeat (and its absence) is considered DEAD.
    """

    def __init__(
        self,
        heartbeats_dir: Path,
        stale_s: float = 10.0,
        dead_s: float = 60.0,
    ) -> None:
        self._heartbeats_dir = heartbeats_dir
        self._stale_s = stale_s
        self._dead_s = dead_s

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read(
        self,
        app_id: str,
    ) -> tuple[Optional[LivenessHeartbeat], Optional[float], bool]:
        """
        Read the heartbeat file for *app_id*.

        Returns
        -------
        (heartbeat, file_mtime_utc, exit_marker_present)

        - ``heartbeat`` is None when the file is absent, unreadable, or corrupt.
        - ``file_mtime_utc`` is ``None`` when ``heartbeat`` is ``None``.
        - ``exit_marker_present`` is True when the marker file exists.
        """
        started_at = time.perf_counter()
        heartbeat_path = self._heartbeats_dir / f"{app_id}.heartbeat.json"
        exit_marker_path = self._heartbeats_dir / f"{app_id}.exit_marker.json"

        if not heartbeat_path.exists():
            self._log_slow_read(app_id, started_at)
            return (None, None, False)

        heartbeat: Optional[LivenessHeartbeat] = None
        file_mtime: Optional[float] = None
        last_err: Optional[Exception] = None

        for attempt in range(_READ_RETRIES):
            try:
                file_mtime = os.path.getmtime(heartbeat_path)
                with open(heartbeat_path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                heartbeat = self._parse_heartbeat(data)
                break
            except (PermissionError, FileNotFoundError) as exc:
                last_err = exc
                if attempt < _READ_RETRIES - 1:
                    time.sleep(_READ_RETRY_DELAY_S)
            except Exception as exc:
                last_err = exc
                log.debug(
                    "HeartbeatReader: failed to parse heartbeat for %s: %s",
                    app_id, exc,
                )
                break

        if heartbeat is None and last_err is not None:
            log.debug(
                "HeartbeatReader: could not read heartbeat for %s after %d attempts: %s",
                app_id, _READ_RETRIES, last_err,
            )

        exit_marker_present = exit_marker_path.exists()
        self._log_slow_read(app_id, started_at)
        return (heartbeat, file_mtime, exit_marker_present)

    def is_stale(self, file_mtime_utc: float) -> bool:
        """Return True if *file_mtime_utc* is older than stale_s."""
        return (time.time() - file_mtime_utc) >= self._stale_s

    def is_dead(self, file_mtime_utc: float) -> bool:
        """Return True if *file_mtime_utc* is older than dead_s."""
        return (time.time() - file_mtime_utc) >= self._dead_s

    def list_known_app_ids(self) -> list[str]:
        """Return app_ids inferred from ``*.heartbeat.json`` filenames."""
        if not self._heartbeats_dir.exists():
            return []
        return [
            p.name.replace(".heartbeat.json", "")
            for p in self._heartbeats_dir.glob("*.heartbeat.json")
        ]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _parse_heartbeat(self, data: dict) -> LivenessHeartbeat:
        """
        Build a LivenessHeartbeat from a raw JSON dict.

        Subclasses may override to deserialize richer domain-specific envelopes
        while still returning something compatible with LivenessHeartbeat.
        """
        return LivenessHeartbeat(
            schema_version=int(data.get("schema_version", 1)),
            app_id=str(data["app_id"]),
            pid=int(data["pid"]),
            ts_wall_utc=float(data["ts_wall_utc"]),
            seq=int(data["seq"]),
        )

    def _log_slow_read(self, app_id: str, started_at: float) -> None:
        elapsed_ms = (time.perf_counter() - started_at) * 1000.0
        if elapsed_ms < _SLOW_HEARTBEAT_READ_THRESHOLD_MS:
            return
        log.warning(
            "HeartbeatReader: slow read for %s took %.1f ms",
            app_id,
            elapsed_ms,
        )
