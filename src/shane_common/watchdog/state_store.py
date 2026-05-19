"""
Generic WatchdogStateStore — manages snapshot and lock files for a watchdog
process.  Backed by atomic JSON writes and an append-only JSONL audit log.

Every except-Exception block uses _report() which calls report_exception()
if available, otherwise traceback.print_exc().  No bare swallowing.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import traceback
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

from .audit import AppendOnlyAuditLog
from .models import BaseLockState

_LOCK_SCHEMA_VERSION = 1


def _report(msg: str) -> None:
    try:
        from trading_platform.utils.diag_logger import report_exception  # type: ignore[import]
        report_exception(msg, kind="exception.watchdog.state_store")
    except Exception:
        print(f"[watchdog.state_store] {msg}")
        traceback.print_exc()


def _atomic_write_json(path: Path, obj: dict) -> None:
    """Write *obj* as compact JSON to *path* atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.stem + "_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
        _replace_with_retry(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _replace_with_retry(src: str, dst: Path, retries: int = 5, delay: float = 0.05) -> None:
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(delay)


class WatchdogStateStore:
    """
    Manages persistent state for a watchdog/supervisor process.

    Layout under *root_dir*:
    ::

        root_dir/
            locks/
                global.lock.json
                watchdog.snapshot.json
            audit/
                watchdog.audit.jsonl

    Parameters
    ----------
    root_dir:
        Root directory for all watchdog state files.
    """

    def __init__(self, root_dir: Path) -> None:
        self._root_dir = root_dir
        locks_dir = root_dir / "locks"
        audit_dir = root_dir / "audit"

        self._lock_path = locks_dir / "global.lock.json"
        self._snapshot_path = locks_dir / "watchdog.snapshot.json"
        self._audit_log = AppendOnlyAuditLog(audit_dir / "watchdog.audit.jsonl")

        self._write_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def append_audit(self, record: dict) -> None:
        """Append *record* to the audit JSONL log."""
        self._audit_log.append(record)

    def tail_audit(self, n: int = 50) -> list[dict]:
        """Return the last *n* audit records."""
        return self._audit_log.tail(n)

    # ------------------------------------------------------------------
    # Lock state
    # ------------------------------------------------------------------

    def load_lock(self) -> BaseLockState:
        """
        Load the global lock state from disk.

        Fail-closed: absent → all-clear default.
        Corrupt → locked with manual_intervention_required=True.
        """
        if not self._lock_path.exists():
            return BaseLockState()
        try:
            with open(self._lock_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return self._parse_lock(data)
        except Exception:
            _report(
                f"WatchdogStateStore.load_lock: corrupt lock file at {self._lock_path} "
                "— defaulting to locked+manual_intervention_required"
            )
            return BaseLockState(locked=True, manual_intervention_required=True)

    def save_lock(self, lock: BaseLockState) -> None:
        """Persist *lock* to disk atomically."""
        with self._write_lock:
            try:
                payload = self._lock_to_dict(lock)
                _atomic_write_json(self._lock_path, payload)
            except Exception:
                _report("WatchdogStateStore.save_lock: write failed")

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def load_snapshot(self) -> dict:
        """Load the watchdog snapshot dict (returns {} if absent/corrupt)."""
        if not self._snapshot_path.exists():
            return {}
        try:
            with open(self._snapshot_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            _report(
                f"WatchdogStateStore.load_snapshot: could not read {self._snapshot_path}"
            )
            return {}

    def save_snapshot(self, snapshot: dict) -> None:
        """Persist *snapshot* to disk atomically."""
        with self._write_lock:
            try:
                _atomic_write_json(self._snapshot_path, snapshot)
            except Exception:
                _report("WatchdogStateStore.save_snapshot: write failed")

    # ------------------------------------------------------------------
    # Subclass hooks
    # ------------------------------------------------------------------

    def _parse_lock(self, data: dict) -> BaseLockState:
        """
        Deserialize a raw dict to a lock state object.

        Subclasses override to produce richer lock state types while still
        calling super()._parse_lock(data) for base fields.
        """
        return BaseLockState(
            locked=bool(data.get("locked", False)),
            manual_intervention_required=bool(
                data.get("manual_intervention_required", False)
            ),
            reason=data.get("reason"),
        )

    def _lock_to_dict(self, lock: BaseLockState) -> dict:
        """
        Serialize a lock state object to a plain dict for JSON storage.

        Subclasses override to add domain-specific fields.
        """
        return {
            "schema_version": _LOCK_SCHEMA_VERSION,
            "locked": lock.locked,
            "manual_intervention_required": lock.manual_intervention_required,
            "reason": lock.reason,
        }
