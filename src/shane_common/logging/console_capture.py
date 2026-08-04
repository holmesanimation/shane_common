# shane_common/logging/console_capture.py
"""
ConsoleCapture — tee sys.stdout / sys.stderr to a rolling CSV log file.

See shane_common/docs/logging.md for full usage guide.

Intercepts all text written to stdout and stderr.  Every line is:
  1. Passed through to the original stream unchanged.
  2. Appended as a CSV row to a ``console_<YYYYMMDD_HHMMSS>.csv`` file.

CSV columns:
    Time, Level, Message

Lifecycle
---------
::

    # Typically called once at process startup (e.g. in package __init__).
    ConsoleCapture.start()           # idempotent

    # Relocate the file once app.logs_root / run_id are known.
    ConsoleCapture.relocate(
        os.path.join(app.logs_root, app.__class__.__name__, app.run_id),
        filename=f"console_{app.run_id}",
    )

    # Teardown (optional — file is flushed/closed on process exit).
    ConsoleCapture.stop()
"""
from __future__ import annotations

import csv
import glob
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from typing import IO, Optional

__all__ = ["ConsoleCapture"]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STDOUT_KIND = "console.stdout"
_STDERR_KIND = "console.stderr"

_LEVEL_RE = re.compile(r"(?<![A-Z])(TRACE|DEBUG|INFO|WARN|ERROR)(?![A-Z])")

_CSV_HEADER = ["Time", "Level", "Message"]


# ---------------------------------------------------------------------------
# _TeeWriter
# ---------------------------------------------------------------------------

class _TeeWriter:
    """
    Wraps a stream and tees every ``write()`` call to ``ConsoleCapture``.

    All attributes not explicitly overridden are forwarded to the underlying
    stream via ``__getattr__``, so the wrapper is transparent to any code
    that probes stream capabilities (e.g. ``isatty()``, ``encoding``,
    ``fileno()``).
    """

    def __init__(self, original: IO, kind: str) -> None:
        object.__setattr__(self, "_original", original)
        object.__setattr__(self, "_kind", kind)

    def write(self, text: str) -> int:
        original = object.__getattribute__(self, "_original")
        kind = object.__getattribute__(self, "_kind")

        try:
            n = original.write(text)
        except Exception:
            n = 0

        try:
            if text:
                for line in text.split("\n"):
                    stripped = line.rstrip("\r")
                    # Skip Python 3.11+ fine-grained error location lines
                    # (lines consisting only of ^ and ~ characters).
                    if stripped and not all(c in "^~ " for c in stripped):
                        ConsoleCapture._append(stripped, kind)
        except Exception:
            pass

        return n

    def flush(self) -> None:
        original = object.__getattribute__(self, "_original")
        try:
            original.flush()
        except Exception:
            pass
        try:
            ConsoleCapture._flush_csv()
        except Exception:
            pass

    def __getattr__(self, name: str):
        original = object.__getattribute__(self, "_original")
        return getattr(original, name)


class _ConsoleCsvLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
        except Exception:
            try:
                message = record.getMessage()
            except Exception:
                return
        ConsoleCapture._append(
            message,
            _STDERR_KIND if record.levelno >= logging.ERROR else _STDOUT_KIND,
        )


# ---------------------------------------------------------------------------
# ConsoleCapture
# ---------------------------------------------------------------------------

class ConsoleCapture:
    """
    Singleton that installs tee-writers over sys.stdout and sys.stderr and
    writes every captured line as a CSV row.

    All public methods are class-level and thread-safe.
    """

    _lock: threading.Lock = threading.Lock()
    _active: bool = False

    # RLock (not Lock): relocate() holds this lock for its whole body, and on a
    # failed move it writes a warning via sys.stderr, which is tee'd through
    # _TeeWriter back into _append() on the same thread — that reentrant
    # _csv_lock acquisition would self-deadlock forever with a plain Lock.
    _csv_lock: threading.RLock = threading.RLock()
    _csv_file = None
    _csv_writer = None
    _csv_path: Optional[str] = None

    _orig_stdout: Optional[IO] = None
    _orig_stderr: Optional[IO] = None
    _logging_handler: Optional[logging.Handler] = None
    _pending_rows: int = 0
    _last_flush_monotonic: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def start(cls, log_dir: Optional[str] = None) -> None:
        """
        Activate the console capture.

        Idempotent — safe to call more than once; subsequent calls are
        no-ops while the capture is already active.

        Parameters
        ----------
        log_dir:
            Directory for the CSV file.  Defaults to
            ``<TEMP>/shane_common/logs/``.
        """
        with cls._lock:
            if cls._active:
                return

            if log_dir is None:
                log_dir = os.path.join(
                    tempfile.gettempdir(), "shane_common", "logs"
                )

            cls._prune(log_dir)

            date_str = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
            filename = f"console_{date_str}.csv"
            csv_path = os.path.join(log_dir, filename)

            try:
                os.makedirs(log_dir, exist_ok=True)
                f = open(csv_path, "w", newline="", encoding="utf-8", buffering=1)
                writer = csv.writer(f)
                writer.writerow(_CSV_HEADER)
                f.flush()
            except Exception as exc:
                try:
                    sys.stderr.write(
                        f"ConsoleCapture: failed to open {csv_path!r}: {exc}\n"
                    )
                except Exception:
                    pass
                return

            with cls._csv_lock:
                cls._csv_file = f
                cls._csv_writer = writer
                cls._csv_path = csv_path
                cls._pending_rows = 0
                cls._last_flush_monotonic = time.perf_counter()

            cls._orig_stdout = sys.stdout
            cls._orig_stderr = sys.stderr

            sys.stdout = _TeeWriter(cls._orig_stdout, _STDOUT_KIND)
            sys.stderr = _TeeWriter(cls._orig_stderr, _STDERR_KIND)

            cls._install_logging_bridge()
            cls._active = True

    @classmethod
    def stop(cls) -> None:
        """
        Deactivate the console capture and restore the original streams.

        Safe to call even if ``start()`` was never called.
        """
        with cls._lock:
            if not cls._active:
                return

            if cls._orig_stdout is not None:
                sys.stdout = cls._orig_stdout
            if cls._orig_stderr is not None:
                sys.stderr = cls._orig_stderr

            cls._orig_stdout = None
            cls._orig_stderr = None
            cls._active = False
            cls._remove_logging_bridge()

        with cls._csv_lock:
            if cls._csv_file is not None:
                try:
                    cls._csv_file.flush()
                    cls._csv_file.close()
                except Exception:
                    pass
                cls._csv_file = None
                cls._csv_writer = None
                cls._csv_path = None
                cls._pending_rows = 0

    @classmethod
    def relocate(
        cls,
        new_dir: str,
        filename: Optional[str] = None,
    ) -> None:
        """
        Move the active CSV file to *new_dir*.

        Useful for moving the early temp file alongside the app's log
        directory once ``app.logs_root`` and ``app.run_id`` are known::

            ConsoleCapture.relocate(
                os.path.join(self.logs_root, self.__class__.__name__, self.run_id),
                filename=f"console_{self.run_id}",
            )

        If the capture is not active, this is a no-op.
        """
        with cls._csv_lock:
            if cls._csv_path is None or cls._csv_file is None:
                return

            old_path = cls._csv_path
            old_filename = os.path.basename(old_path)

            if filename is not None:
                new_filename = filename if filename.endswith(".csv") else f"{filename}.csv"
            else:
                new_filename = old_filename

            new_path = os.path.join(new_dir, new_filename)

            if os.path.abspath(old_path) == os.path.abspath(new_path):
                return

            try:
                cls._csv_file.flush()
                cls._csv_file.close()
            except Exception:
                pass
            cls._csv_file = None
            cls._csv_writer = None

            # Copy (not rename) the old file into place: renaming needs exclusive
            # delete access, which a transient AV/indexer lock can block for
            # seconds, but a plain read-only copy succeeds even while locked.
            try:
                os.makedirs(new_dir, exist_ok=True)
                shutil.copyfile(old_path, new_path)
                for attempt in range(5):
                    try:
                        os.remove(old_path)
                        break
                    except Exception:
                        if attempt < 4:
                            time.sleep(0.2)
            except Exception as exc:
                try:
                    sys.stderr.write(
                        f"ConsoleCapture.relocate: move failed ({exc}); "
                        f"continuing at {old_path!r}\n"
                    )
                except Exception:
                    pass
                new_path = old_path

            try:
                f = open(new_path, "a", newline="", encoding="utf-8", buffering=1)
                cls._csv_file = f
                cls._csv_writer = csv.writer(f)
                cls._csv_path = new_path
                cls._pending_rows = 0
                cls._last_flush_monotonic = time.perf_counter()
            except Exception as exc:
                try:
                    sys.stderr.write(
                        f"ConsoleCapture.relocate: could not reopen {new_path!r}: {exc}\n"
                    )
                except Exception:
                    pass

    @classmethod
    def _prune(cls, log_dir: str, keep_days: int = 7) -> None:
        """Delete ``console_*.csv`` files in *log_dir* older than *keep_days* days."""
        try:
            cutoff = time.time() - keep_days * 86_400
            pattern = os.path.join(log_dir, "console_*.csv")
            for path in glob.glob(pattern):
                try:
                    if os.path.getmtime(path) < cutoff:
                        os.remove(path)
                except Exception:
                    pass
        except Exception:
            pass

    @property
    def log_path(self) -> Optional[str]:
        """Absolute path of the current CSV file, or ``None`` if inactive."""
        return self.__class__._csv_path

    @classmethod
    def _install_logging_bridge(cls) -> None:
        if cls._logging_handler is not None:
            return
        try:
            handler = _ConsoleCsvLogHandler(level=logging.NOTSET)
            handler.setFormatter(logging.Formatter("%(name)s %(message)s"))
            logger = logging.getLogger("TradeApp")
            logger.addHandler(handler)
            # Most "TradeApp.*" child loggers throughout the app (e.g.
            # SimulationRunner, ReplayController) are created via a plain
            # logging.getLogger(...) call and never call setLevel(), so their
            # *effective* level resolves up the ancestor chain to the root
            # logger's default of WARNING. That silently drops every
            # info()/debug() call before a LogRecord is even created — this
            # handler on "TradeApp" would never see them, no matter how the
            # handler itself is configured. Set an explicit floor here so
            # descendants that don't set their own level inherit INFO instead
            # of the interpreter default. Do not override a level a caller
            # may have already configured on this logger.
            if logger.level == logging.NOTSET:
                logger.setLevel(logging.INFO)
            cls._logging_handler = handler
        except Exception:
            cls._logging_handler = None

    @classmethod
    def _remove_logging_bridge(cls) -> None:
        handler = cls._logging_handler
        if handler is None:
            return
        try:
            logging.getLogger("TradeApp").removeHandler(handler)
        except Exception:
            pass
        cls._logging_handler = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @classmethod
    def _append(cls, line: str, kind: str) -> None:
        """Write one CSV row for *line*. Called from _TeeWriter — must never raise."""
        try:
            wall_ts = time.time()
            ts_iso = datetime.fromtimestamp(wall_ts, tz=timezone.utc).isoformat()

            if kind == _STDERR_KIND:
                level = "ERROR"
            else:
                m = _LEVEL_RE.search(line)
                level = m.group(1) if m else "INFO"

            with cls._csv_lock:
                if cls._csv_writer is None:
                    return
                cls._csv_writer.writerow([ts_iso, level, line])
                cls._pending_rows += 1
                _now = time.perf_counter()
                if cls._pending_rows >= 25 or (_now - cls._last_flush_monotonic) >= 1.0:
                    cls._flush_csv_locked(now_monotonic=_now)
        except Exception:
            pass

    @classmethod
    def _flush_csv(cls) -> None:
        with cls._csv_lock:
            cls._flush_csv_locked(now_monotonic=time.perf_counter())

    @classmethod
    def _flush_csv_locked(cls, *, now_monotonic: float) -> None:
        if cls._csv_file is None:
            return
        try:
            cls._csv_file.flush()
        except Exception:
            return
        cls._pending_rows = 0
        cls._last_flush_monotonic = now_monotonic
