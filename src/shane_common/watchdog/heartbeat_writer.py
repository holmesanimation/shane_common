"""
Generic HeartbeatWriter — daemon thread that periodically writes an atomic
heartbeat JSON file to disk for a watchdog/supervisor process to read.

Thread-safety contract
-----------------------
- One daemon thread owns all file I/O (``_write_loop``).
- ``_seq`` is incremented under its own Lock; it is never read from the
  ``snapshot_fn`` return value, ensuring strict monotonicity.
- Subclasses may override ``_build_payload(seq, ts)`` to add domain-specific
  fields to the heartbeat dict.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_DEFAULT_PERIOD_S: float = 2.0
_REPLACE_RETRIES: int = 5
_REPLACE_DELAY_S: float = 0.05


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------

def _write_atomic(path: Path, data: dict) -> None:
    """Write *data* as compact JSON atomically via a sibling temp file + os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.stem + "_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, separators=(",", ":"))
        _replace_with_retry(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _replace_with_retry(src: str, dst: Path) -> None:
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == _REPLACE_RETRIES - 1:
                raise
            time.sleep(_REPLACE_DELAY_S)


# ---------------------------------------------------------------------------
# HeartbeatWriter
# ---------------------------------------------------------------------------

class HeartbeatWriter:
    """
    Writes ``<heartbeats_dir>/<app_id>.heartbeat.json`` from a daemon thread.

    Parameters
    ----------
    app_id:
        Unique identifier for this application; used as the filename stem.
    heartbeats_dir:
        Directory where heartbeat files are written.
    period_s:
        How often (in seconds) to write a heartbeat.  Default 2.0.
    """

    def __init__(
        self,
        app_id: str,
        heartbeats_dir: Path,
        period_s: float = _DEFAULT_PERIOD_S,
    ) -> None:
        self._app_id = app_id
        self._heartbeats_dir = heartbeats_dir
        self._period_s = period_s

        self._heartbeat_path = heartbeats_dir / f"{app_id}.heartbeat.json"
        self._exit_marker_path = heartbeats_dir / f"{app_id}.exit_marker.json"

        self._seq_lock = threading.Lock()
        self._seq: int = 0

        self._running: bool = False
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # Subclass hook
    # ------------------------------------------------------------------

    def _build_payload(self, seq: int, ts_wall_utc: float) -> dict:
        """
        Build the dict that will be written as the heartbeat JSON.

        Subclasses override this to inject domain-specific fields while
        keeping ``seq`` and ``ts_wall_utc`` authoritative from this base.
        """
        return {
            "schema_version": 1,
            "app_id": self._app_id,
            "pid": os.getpid(),
            "ts_wall_utc": ts_wall_utc,
            "seq": seq,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the daemon write thread."""
        if self._thread is not None and self._thread.is_alive():
            log.warning(
                "HeartbeatWriter.start() called while already running for %s — ignored.",
                self._app_id,
            )
            return
        try:
            if self._exit_marker_path.exists():
                self._exit_marker_path.unlink()
        except OSError:
            log.exception(
                "HeartbeatWriter.start() failed to clear stale exit marker for %s.",
                self._app_id,
            )
        self._running = True
        self._thread = threading.Thread(
            target=self._write_loop,
            name=f"HeartbeatWriter-{self._app_id}",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "HeartbeatWriter started for %s (period=%.1f s).",
            self._app_id,
            self._period_s,
        )

    def stop(self) -> None:
        """
        Signal the write loop to stop and join the thread (up to 5 s).

        After joining, writes a terminal heartbeat and an exit marker.
        """
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            if self._thread.is_alive():
                log.warning(
                    "HeartbeatWriter thread for %s did not stop within 5 s.",
                    self._app_id,
                )
        self._write_once()
        self._write_exit_marker()
        log.info("HeartbeatWriter stopped for %s.", self._app_id)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _next_seq(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    def _write_once(self) -> None:
        seq = self._next_seq()
        ts = time.time()
        try:
            payload = self._build_payload(seq=seq, ts_wall_utc=ts)
            _write_atomic(self._heartbeat_path, payload)
        except Exception:
            log.exception(
                "HeartbeatWriter._write_once failed for %s.", self._app_id
            )

    def _write_exit_marker(self) -> None:
        try:
            marker = {
                "app_id": self._app_id,
                "ts_wall_utc": time.time(),
                "pid": os.getpid(),
            }
            _write_atomic(self._exit_marker_path, marker)
        except Exception:
            log.exception(
                "HeartbeatWriter._write_exit_marker failed for %s.", self._app_id
            )

    def _write_loop(self) -> None:
        log.debug("HeartbeatWriter._write_loop started for %s.", self._app_id)
        while self._running:
            self._write_once()
            time.sleep(self._period_s)
        log.debug("HeartbeatWriter._write_loop exiting for %s.", self._app_id)
