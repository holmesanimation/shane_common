"""Application-neutral backup-management contract (M5-2): job vocabulary, health/staleness semantics, cross-process single-flight, durable job history.

Promoted from the neutral parts of Purity's backup subsystem (run-status vocabulary, auth states, staleness rule, single-flight behaviour) WITHOUT importing or modifying
Purity. Nothing here knows about Dropbox, MarketData or any application: an application supplies the engine and the policy; this module only gives UIs and services a shared
shape. A job record is a plain dict-serialisable dataclass so a console, a CLI and a scheduler can all read the same history.
"""

from __future__ import annotations

import enum
import json
import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from shane_common.io.atomic import write_json_atomic


class RunStatus(str, enum.Enum):
    """Purity's vocabulary for a finished run."""
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


class AuthState(str, enum.Enum):
    """Purity's credential/connection vocabulary, independent of any provider."""
    DISABLED = "disabled"
    AUTH_REQUIRED = "auth_required"
    READY = "ready"
    REAUTH_REQUIRED = "reauth_required"


class JobKind(str, enum.Enum):
    SYNC_ARCHIVE = "sync_archive"
    SYNC_CLOUD = "sync_cloud"
    PREVIEW = "preview"
    VERIFY = "verify"
    TEST_RESTORE = "test_restore"


class JobState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"                  # stopped by the owner at a safe boundary: verified records kept, upload progress resumable, NEVER a success
    REJECTED = "rejected"                    # refused by policy (not authorized / unavailable / busy): nothing ran

    @property
    def finished(self) -> bool:
        return self not in (JobState.QUEUED, JobState.RUNNING)


@dataclass
class JobRecord:
    job_id: str
    kind: str
    source: str
    state: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    requested_by: str = "console"
    scope: str = ""
    cancel_requested: bool = False
    files_total: int = 0
    files_done: int = 0
    bytes_sent: int = 0
    verified: int = 0
    failed: int = 0
    current: Optional[str] = None
    message: Optional[str] = None
    reasons: list = field(default_factory=list)
    failures: list = field(default_factory=list)            # [(path, outcome, kind, message, retriable)] bounded by the producer
    result: dict = field(default_factory=dict)              # kind-specific summary (counts, verdict, evidence file)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "JobRecord":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in names})


def utc_iso(ts: Optional[float] = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------------------------------- health (pure; Purity semantics)
def last_successful(records: list, kind: Optional[str] = None, source: Optional[str] = None) -> Optional[JobRecord]:
    best = None
    for r in sorted(records, key=lambda x: x.completed_at or x.started_at or ""):
        if r.state == JobState.SUCCESS.value and (kind is None or r.kind == kind) and (source is None or r.source == source):
            best = r
    return best


def is_stale(records: list, threshold_days: float, *, kind: Optional[str] = None, source: Optional[str] = None, now: Optional[datetime] = None) -> bool:
    """True if there is no successful run of that kind/source, or the last one is older than the threshold (same rule as Purity's ``is_stale``)."""
    last = last_successful(records, kind, source)
    if last is None:
        return True
    try:
        done = datetime.fromisoformat(last.completed_at or last.started_at or "")
    except ValueError:
        return True
    if done.tzinfo is None:
        done = done.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - done).total_seconds() / 86400.0 > threshold_days


# ---------------------------------------------------------------------------------------------------- single flight (cross-process, stale-safe)
class SingleFlightBusy(Exception):
    def __init__(self, holder: dict) -> None:
        super().__init__("another backup job is running")
        self.holder = holder


class FileSingleFlight:
    """One job at a time across processes. The lock file holds ``{pid, job_id, heartbeat}``; a lock whose heartbeat is older than ``stale_after`` seconds (crashed holder) is taken
    over. The holder calls ``heartbeat()`` while working and ``release()`` when done."""

    def __init__(self, path: Path | str, *, stale_after: float = 300.0, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._stale = stale_after
        self._clock = clock
        self._tlock = threading.Lock()
        self._token: Optional[str] = None

    def _read(self) -> Optional[dict]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def acquire(self, job_id: str) -> None:
        with self._tlock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            token = uuid.uuid4().hex
            body = json.dumps({"pid": os.getpid(), "job_id": job_id, "heartbeat": self._clock(), "token": token})
            for _ in range(2):
                try:
                    fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                except FileExistsError:
                    holder = self._read()
                    if holder is not None and self._clock() - float(holder.get("heartbeat", 0)) <= self._stale:
                        raise SingleFlightBusy(holder)
                    try:
                        os.unlink(self._path)                          # stale or unreadable lock: recover
                    except OSError:
                        raise SingleFlightBusy(holder or {})
                    continue
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(body)
                self._token = token
                return
            raise SingleFlightBusy(self._read() or {})

    def heartbeat(self) -> None:
        with self._tlock:
            cur = self._read()
            if cur and cur.get("token") == self._token:
                cur["heartbeat"] = self._clock()
                write_json_atomic(self._path, cur)

    def release(self) -> None:
        with self._tlock:
            cur = self._read()
            if cur and cur.get("token") == self._token:
                try:
                    os.unlink(self._path)
                except OSError:
                    pass
            self._token = None

    def holder(self) -> Optional[dict]:
        cur = self._read()
        if cur is None or self._clock() - float(cur.get("heartbeat", 0)) > self._stale:
            return None
        return {k: v for k, v in cur.items() if k != "token"}


# ---------------------------------------------------------------------------------------------------- durable job history
class JobStore:
    """One JSON file per job (atomic replace), so a crash never corrupts history and progress is readable without the host running. Unreadable files are skipped, never fatal."""

    def __init__(self, directory: Path | str, *, keep: int = 500) -> None:
        self._dir = Path(directory)
        self._keep = keep

    def path_of(self, job_id: str) -> Path:
        if not self._valid_id(job_id):
            raise ValueError("invalid job id")
        return self._dir / f"{job_id}.json"

    def save(self, rec: JobRecord) -> None:
        write_json_atomic(self._dir / f"{rec.job_id}.json", rec.to_dict())

    # -- cooperative cancellation: a flag FILE (not part of the job record, so it can be set from any process without racing the worker's saves)
    @staticmethod
    def _valid_id(job_id: str) -> bool:
        return bool(job_id) and job_id.replace("-", "").replace("_", "").isalnum()

    def request_cancel(self, job_id: str) -> None:
        if not self._valid_id(job_id):
            raise ValueError("invalid job id")
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / f"{job_id}.cancel").write_text(utc_iso(), encoding="utf-8")

    def cancel_requested(self, job_id: str) -> bool:
        return self._valid_id(job_id) and (self._dir / f"{job_id}.cancel").exists()

    def clear_cancel(self, job_id: str) -> None:
        if self._valid_id(job_id):
            try:
                (self._dir / f"{job_id}.cancel").unlink()
            except OSError:
                pass

    def get(self, job_id: str) -> Optional[JobRecord]:
        if not self._valid_id(job_id):
            return None
        r = None
        for attempt in range(4):                                  # the worker replaces the file atomically; on Windows a reader can briefly lose the race
            try:
                r = JobRecord.from_dict(json.loads((self._dir / f"{job_id}.json").read_text(encoding="utf-8")))
                break
            except (OSError, ValueError, TypeError):
                time.sleep(0.05 * (attempt + 1))
        if r is None:
            return None
        r.cancel_requested = self.cancel_requested(job_id) and not JobState(r.state).finished
        return r

    def list_recent(self, limit: int = 50) -> list:
        if not self._dir.is_dir():
            return []
        out = []
        for p in self._dir.glob("*.json"):
            try:
                out.append(JobRecord.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except (OSError, ValueError, TypeError):
                continue
        out.sort(key=lambda r: r.started_at or "", reverse=True)
        for r in out[:limit]:
            r.cancel_requested = self.cancel_requested(r.job_id) and not JobState(r.state).finished
        return out[:limit]

    def mark_interrupted(self, message: str = "interrupted: the service stopped while this job was running") -> int:
        """On start-up: any job still queued/running belongs to a dead process -> failed (never left looking alive)."""
        n = 0
        for r in self.list_recent(self._keep):
            if not JobState(r.state).finished:
                r.state, r.completed_at, r.message = JobState.FAILED.value, utc_iso(), message
                self.save(r)
                n += 1
        return n

    def prune(self) -> None:
        recs = self.list_recent(10_000)
        for r in recs[self._keep:]:
            try:
                (self._dir / f"{r.job_id}.json").unlink()
            except OSError:
                pass


def new_job_id(clock: Callable[[], float] = time.time) -> str:
    return f"{int(clock())}-{uuid.uuid4().hex[:8]}"
