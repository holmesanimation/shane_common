"""Application-neutral verified Dropbox destination (M5-1).

Promotes the proven mechanisms of Purity's Dropbox leg (Dropbox content hash = SHA-256 of 4 MiB block SHA-256s; refresh-token auth; explicit conflict states) into a shared,
dependency-free-of-applications module, adding resumable upload sessions, retries and an independent post-upload verification.

Verification rule: an upload is VERIFIED only when a SEPARATE ``files_get_metadata`` call reports a ``content_hash`` equal to the Dropbox content hash computed locally BEFORE the
upload, and the source did not change (size, mtime_ns) while it was being read/sent. The upload response alone is never accepted.

Remote policy (no silent overwrite): remote absent -> add; remote hash equals source -> ALREADY_PRESENT_VERIFIED (nothing sent); remote differs but equals the hash the CALLER has
on record as last verified there -> overwrite with ``update(rev)`` (REFRESHED); remote differs from both -> CONFLICT, nothing written. Nothing here deletes remote files.

Local fingerprint state and remote verification state are the caller's separate concerns; this module only reports what Dropbox itself says.
"""

from __future__ import annotations

import enum
import hashlib
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

import dropbox
from dropbox.exceptions import ApiError, AuthError, BadInputError, DropboxException, HttpError, InternalServerError, RateLimitError
from dropbox.files import CommitInfo, FileMetadata, FolderMetadata, UploadSessionCursor, WriteMode

BLOCK = 4 * 1024 * 1024
MIB = 1024 * 1024


# ------------------------------------------------------------------------------------------------ hashing
def dropbox_content_hash_stream(read: Callable[[int], bytes]) -> str:
    blocks = bytearray()
    while True:
        chunk = read(BLOCK)
        if not chunk:
            break
        blocks.extend(hashlib.sha256(chunk).digest())
    return hashlib.sha256(bytes(blocks)).hexdigest()


def dropbox_content_hash(path: Path | str) -> str:
    with open(path, "rb") as fh:
        return dropbox_content_hash_stream(fh.read)


def hash_file_both(path: Path | str) -> tuple[str, str, int]:
    """One pass over the file -> (sha256 hex, dropbox_content_hash hex, size). SHA-256 is the local/restore evidence; the Dropbox hash is what the remote reports."""
    sha, blocks, size = hashlib.sha256(), bytearray(), 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(BLOCK)
            if not chunk:
                break
            size += len(chunk)
            sha.update(chunk)
            blocks.extend(hashlib.sha256(chunk).digest())
    return sha.hexdigest(), hashlib.sha256(bytes(blocks)).hexdigest(), size


# ------------------------------------------------------------------------------------------------ results
class UploadOutcome(str, enum.Enum):
    VERIFIED_UPLOADED = "VERIFIED_UPLOADED"
    ALREADY_PRESENT_VERIFIED = "ALREADY_PRESENT_VERIFIED"
    REFRESHED_VERIFIED = "REFRESHED_VERIFIED"
    CONFLICT = "CONFLICT"                    # remote differs from the source AND from the caller's last verified evidence: nothing written
    HASH_MISMATCH = "HASH_MISMATCH"          # uploaded, but Dropbox independently reports a different content hash: NOT verified
    SOURCE_CHANGED = "SOURCE_CHANGED"        # the source changed while being hashed/sent: NOT verified, retry later
    SOURCE_UNREADABLE = "SOURCE_UNREADABLE"
    UNVERIFIED = "UNVERIFIED"                # bulk path: neither the recursive listing nor the targeted metadata lookup established the expected hash; nothing is claimed
    CANCELLED = "CANCELLED"                  # the caller asked to stop at a safe boundary: nothing is claimed verified; session progress is kept so the file can resume
    FAILED = "FAILED"


_VERIFIED = {UploadOutcome.VERIFIED_UPLOADED, UploadOutcome.ALREADY_PRESENT_VERIFIED, UploadOutcome.REFRESHED_VERIFIED}


@dataclass(frozen=True)
class DropboxFailure:
    kind: str                                # auth | rate_limit | network | server | insufficient_space | path | api | other
    message: str
    retriable: bool


@dataclass(frozen=True)
class RemoteFile:
    path: str
    size: int
    content_hash: Optional[str]
    rev: Optional[str]


@dataclass(frozen=True)
class UploadResult:
    outcome: UploadOutcome
    remote_path: str
    size: int = 0
    sha256: Optional[str] = None
    dropbox_hash: Optional[str] = None       # computed locally before upload
    remote_hash: Optional[str] = None        # reported by Dropbox in the independent verification call
    remote_rev: Optional[str] = None
    bytes_sent: int = 0
    attempts: int = 0
    resumed: bool = False
    source_size: int = 0
    source_mtime_ns: int = 0
    failure: Optional[DropboxFailure] = None

    @property
    def verified(self) -> bool:
        return self.outcome in _VERIFIED and self.remote_hash is not None and self.remote_hash == self.dropbox_hash


@dataclass(frozen=True)
class RemoteCheck:
    state: str                               # OK | MISSING | MISMATCH | NOT_A_FILE | ERROR
    remote_hash: Optional[str] = None
    failure: Optional[DropboxFailure] = None


@dataclass(frozen=True)
class DownloadResult:
    """Download for independent restore verification (M6 consumes this; a successful download here is NOT a restore drill)."""
    local_path: str
    size: int
    sha256: str
    dropbox_hash: str
    remote_hash: Optional[str]
    remote_hash_ok: bool


class _Cancelled(Exception):
    pass


class DropboxAuthInvalid(Exception):
    """The credential was rejected (revoked / expired refresh token / insufficient scope): re-authorization is needed. Never retried."""


class ProgressStore(Protocol):
    def get(self, key: str) -> Optional[dict]: ...

    def put(self, key: str, value: dict) -> None: ...

    def delete(self, key: str) -> None: ...


class InMemoryProgress:
    def __init__(self) -> None:
        self.d: dict = {}

    def get(self, key: str) -> Optional[dict]:
        return self.d.get(key)

    def put(self, key: str, value: dict) -> None:
        self.d[key] = dict(value)

    def delete(self, key: str) -> None:
        self.d.pop(key, None)


class LedgerProgress:
    """Restart-safe upload-session progress on top of the generic durable JSON ledger (a handful of in-flight records)."""

    def __init__(self, path: Path | str) -> None:
        from shane_common.runtime.transfer.manifest_store import DurableRecordLedger
        self._l = DurableRecordLedger(Path(path))
        self._lock = threading.Lock()                       # concurrent upload workers share this ledger: whole-file read-modify-write must be serialized

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            return self._l.get(key)

    def put(self, key: str, value: dict) -> None:
        with self._lock:
            self._l.upsert(key, dict(value))

    def delete(self, key: str) -> None:
        with self._lock:
            self._l.delete(key)


# ------------------------------------------------------------------------------------------------ destination
def _classify(exc: BaseException) -> DropboxFailure:
    if isinstance(exc, RateLimitError):
        return DropboxFailure("rate_limit", "rate limited", True)
    if isinstance(exc, InternalServerError):
        return DropboxFailure("server", f"server error {getattr(exc, 'status_code', '?')}", True)
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)) and not isinstance(exc, (FileNotFoundError, PermissionError)):
        return DropboxFailure("network", f"{type(exc).__name__}: {exc}", True)
    if isinstance(exc, HttpError):
        return DropboxFailure("server", f"http {getattr(exc, 'status_code', '?')}", True)
    if isinstance(exc, ApiError):
        text = str(getattr(exc, "error", exc))
        if "insufficient_space" in text:
            return DropboxFailure("insufficient_space", "Dropbox is full", False)
        return DropboxFailure("api", text[:300], False)
    return DropboxFailure("other", f"{type(exc).__name__}: {exc}"[:300], False)


class Throttle:
    """Shared backoff gate for concurrent workers: when Dropbox rate-limits one request every worker pauses (one decision, not N independent retries)."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        self._clock, self._sleep, self._until, self._lock = clock, sleep, 0.0, threading.Lock()
        self.events = 0

    def wait(self) -> None:
        with self._lock:
            d = self._until - self._clock()
        if d > 0:
            self._sleep(d)

    def penalize(self, seconds: float) -> None:
        with self._lock:
            self._until = max(self._until, self._clock() + seconds)
            self.events += 1


def _source_unreadable(local: Path) -> bool:
    try:
        with open(local, "rb") as fh:
            fh.read(1)
        return False
    except OSError:
        return True


class DropboxDestination:
    def __init__(self, client: Any, *, remote_root: str = "", single_shot_limit: int = 32 * MIB, chunk_size: int = 16 * MIB, max_attempts: int = 5,
                 progress: Optional[ProgressStore] = None, sleep: Callable[[float], None] = time.sleep, backoff_base: float = 1.0,
                 throttle: Optional[Throttle] = None) -> None:
        if chunk_size % BLOCK:
            raise ValueError("chunk_size must be a multiple of 4 MiB")
        self._c = client
        self._root = "/" + remote_root.strip("/") if remote_root.strip("/") else ""
        self._single = single_shot_limit
        self._chunk = chunk_size
        self._max = max_attempts
        self._progress = progress if progress is not None else InMemoryProgress()
        self._sleep = sleep
        self._backoff = backoff_base
        self._throttle = throttle
        self._stats_lock = threading.Lock()
        self.stats = {"retries": 0, "rate_limits": 0}

    @property
    def client(self) -> Any:
        return self._c

    # -- paths ---------------------------------------------------------------------------------------------------------------------
    def remote_path(self, rel: str) -> str:
        if not rel or rel.startswith("/") or "\\" in rel or ":" in rel or any(p in ("", ".", "..") for p in rel.split("/")):
            raise ValueError(f"unsafe relative remote path: {rel!r}")
        return f"{self._root}/{rel}"

    # -- retry wrapper ---------------------------------------------------------------------------------------------------------------
    def _call(self, fn: Callable[[], Any]) -> tuple[Any, int]:
        attempts = 0
        while True:
            attempts += 1
            if self._throttle is not None:
                self._throttle.wait()
            try:
                return fn(), attempts
            except AuthError as e:
                raise DropboxAuthInvalid(str(e)) from e
            except BadInputError as e:
                if "invalid_grant" in str(e) or "invalid_access_token" in str(e):
                    raise DropboxAuthInvalid(str(e)) from e
                raise
            except (RateLimitError, InternalServerError, HttpError, ConnectionError, TimeoutError) as e:
                if attempts >= self._max:
                    raise
                wait = min(float(getattr(e, "backoff", None) or self._backoff * (2 ** (attempts - 1))), 60.0)
                with self._stats_lock:
                    self.stats["retries"] += 1
                    self.stats["rate_limits"] += isinstance(e, RateLimitError)
                if isinstance(e, RateLimitError) and self._throttle is not None:
                    self._throttle.penalize(wait)
                self._sleep(wait)

    # -- remote reads ----------------------------------------------------------------------------------------------------------------
    def get_remote(self, rel: str) -> Optional[RemoteFile]:
        path = self.remote_path(rel)
        try:
            md, _ = self._call(lambda: self._c.files_get_metadata(path))
        except ApiError as e:
            err = getattr(e, "error", None)
            if err is not None and hasattr(err, "is_path") and err.is_path() and err.get_path().is_not_found():
                return None
            raise
        if isinstance(md, FolderMetadata):
            return RemoteFile(path, -1, None, None)
        return RemoteFile(path, int(md.size), getattr(md, "content_hash", None), getattr(md, "rev", None))

    def verify_remote(self, rel: str, expected_dropbox_hash: str) -> RemoteCheck:
        """Metadata-only re-verification of a previously verified file (detects a deleted or altered remote)."""
        try:
            r = self.get_remote(rel)
        except DropboxAuthInvalid:
            raise
        except (DropboxException, OSError) as e:
            return RemoteCheck("ERROR", failure=_classify(e))
        if r is None:
            return RemoteCheck("MISSING")
        if r.size < 0:
            return RemoteCheck("NOT_A_FILE")
        return RemoteCheck("OK" if r.content_hash == expected_dropbox_hash else "MISMATCH", r.content_hash)

    def download_to(self, rel: str, dest: Path | str) -> DownloadResult:
        """Download for M6: bytes are hashed locally (SHA-256 + Dropbox hash) and compared with Dropbox's independently reported hash."""
        path = self.remote_path(rel)
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        md, _ = self._call(lambda: self._c.files_download_to_file(str(dest), path))
        sha, dbx, size = hash_file_both(dest)
        remote_hash = getattr(md, "content_hash", None)
        return DownloadResult(str(dest), size, sha, dbx, remote_hash, remote_hash is not None and remote_hash == dbx)

    # -- verified store --------------------------------------------------------------------------------------------------------------
    def store_file(self, local_path: Path | str, rel: str, *, recorded_remote_hash: Optional[str] = None, allow_refresh: bool = True,
                   should_cancel: Optional[Callable[[], bool]] = None) -> UploadResult:
        local = Path(local_path)
        remote_path = self.remote_path(rel)
        try:
            st1 = os.stat(local)
            sha, dbx, size = hash_file_both(local)
            st2 = os.stat(local)
        except OSError as e:
            return UploadResult(UploadOutcome.SOURCE_UNREADABLE, remote_path, failure=DropboxFailure("other", f"{type(e).__name__}: {e}"[:300], False))
        base = dict(remote_path=remote_path, size=size, sha256=sha, dropbox_hash=dbx, source_size=st1.st_size, source_mtime_ns=st1.st_mtime_ns)
        if (st1.st_size, st1.st_mtime_ns) != (st2.st_size, st2.st_mtime_ns) or size != st1.st_size:
            return UploadResult(UploadOutcome.SOURCE_CHANGED, **base)
        attempts = 0
        try:
            remote = self.get_remote(rel)
            if remote is not None and remote.size < 0:
                return UploadResult(UploadOutcome.CONFLICT, **base, failure=DropboxFailure("path", "a folder exists at the file path", False))
            if remote is None:
                mode, refreshed = WriteMode.add, False
            elif remote.content_hash == dbx:
                return UploadResult(UploadOutcome.ALREADY_PRESENT_VERIFIED, **base, remote_hash=remote.content_hash, remote_rev=remote.rev)
            elif allow_refresh and recorded_remote_hash is not None and remote.content_hash == recorded_remote_hash:
                mode, refreshed = WriteMode.update(remote.rev), True
            else:
                return UploadResult(UploadOutcome.CONFLICT, **base, remote_hash=remote.content_hash, remote_rev=remote.rev)
            if should_cancel is not None and should_cancel():
                return UploadResult(UploadOutcome.CANCELLED, **base, remote_hash=remote.content_hash if remote else None)
            sent, resumed, attempts = self._send(local, remote_path, mode, size, st1, dbx, should_cancel=should_cancel)
            # independent verification: a separate metadata call, never the upload response
            md, a2 = self._call(lambda: self._c.files_get_metadata(remote_path))
            attempts += a2
            remote_hash, rev = getattr(md, "content_hash", None), getattr(md, "rev", None)
            st3 = os.stat(local)
        except DropboxAuthInvalid:
            raise
        except _Cancelled:
            return UploadResult(UploadOutcome.CANCELLED, **base, attempts=attempts)
        except OSError as e:
            if isinstance(e, (FileNotFoundError, PermissionError)) and _source_unreadable(local):           # only when the SOURCE itself is the problem
                return UploadResult(UploadOutcome.SOURCE_UNREADABLE, **base, attempts=attempts, failure=DropboxFailure("other", f"{type(e).__name__}: {e}"[:300], False))
            return UploadResult(UploadOutcome.FAILED, **base, attempts=attempts, failure=_classify(e))
        except (DropboxException, ConnectionError, TimeoutError) as e:
            return UploadResult(UploadOutcome.FAILED, **base, attempts=attempts, failure=_classify(e))
        base.update(remote_hash=remote_hash, remote_rev=rev, bytes_sent=sent, attempts=attempts, resumed=resumed)
        if (st3.st_size, st3.st_mtime_ns) != (st1.st_size, st1.st_mtime_ns):
            return UploadResult(UploadOutcome.SOURCE_CHANGED, **base)
        if remote_hash != dbx:
            return UploadResult(UploadOutcome.HASH_MISMATCH, **base)
        return UploadResult(UploadOutcome.REFRESHED_VERIFIED if refreshed else UploadOutcome.VERIFIED_UPLOADED, **base)

    # -- transport ---------------------------------------------------------------------------------------------------------------------
    def _send(self, local: Path, remote_path: str, mode: Any, size: int, st: os.stat_result, dbx: str, _resume: bool = True,
              should_cancel: Optional[Callable[[], bool]] = None) -> tuple[int, bool, int]:
        attempts = 0
        if size <= self._single:
            data = local.read_bytes()
            _, attempts = self._call(lambda: self._c.files_upload(data, remote_path, mode=mode, mute=True))
            return len(data), False, attempts
        key = remote_path
        prog = self._progress.get(key)
        offset, session_id, resumed = 0, None, False
        if _resume and prog and prog.get("size") == size and prog.get("mtime_ns") == st.st_mtime_ns and prog.get("dbx") == dbx and int(prog.get("offset", 0)) <= size:
            session_id, offset, resumed = prog["session_id"], int(prog["offset"]), True
        elif prog:
            self._progress.delete(key)                                         # source changed since the session began: discard it
        sent = 0
        with open(local, "rb") as fh:
            if session_id is None:
                first = fh.read(self._chunk)
                r, a = self._call(lambda: self._c.files_upload_session_start(first))
                attempts += a
                session_id, offset, sent = r.session_id, len(first), len(first)
                self._progress.put(key, {"session_id": session_id, "offset": offset, "size": size, "mtime_ns": st.st_mtime_ns, "dbx": dbx})
            while size - offset > self._chunk:
                fh.seek(offset)
                chunk = fh.read(self._chunk)
                cursor = UploadSessionCursor(session_id, offset)
                try:
                    _, a = self._call(lambda: self._c.files_upload_session_append_v2(chunk, cursor))
                    attempts += a
                except ApiError as e:
                    fixed = self._repair_offset(e)
                    if fixed is None:
                        self._progress.delete(key)
                        if resumed:                                              # expired / closed session: start over once
                            return self._send(local, remote_path, mode, size, st, dbx, _resume=False, should_cancel=should_cancel)
                        raise
                    offset = fixed                                              # server is at a different offset: realign and continue
                    continue
                offset += len(chunk)
                sent += len(chunk)
                self._progress.put(key, {"session_id": session_id, "offset": offset, "size": size, "mtime_ns": st.st_mtime_ns, "dbx": dbx})
                if should_cancel is not None and should_cancel():
                    raise _Cancelled()                                           # progress is already persisted: the next run resumes at this offset
            fh.seek(offset)
            last = fh.read()
            cursor = UploadSessionCursor(session_id, offset)
            commit = CommitInfo(remote_path, mode=mode, mute=True)
            try:
                _, a = self._call(lambda: self._c.files_upload_session_finish(last, cursor, commit))
            except ApiError as e:
                fixed = self._repair_offset(e)
                self._progress.delete(key)
                if fixed is None:
                    if resumed:
                        return self._send(local, remote_path, mode, size, st, dbx, _resume=False, should_cancel=should_cancel)
                    raise
                raise ConnectionError("upload session offset mismatch at finish; restart") from e
            attempts += a
            sent += len(last)
        self._progress.delete(key)
        return sent, resumed, attempts

    @staticmethod
    def _repair_offset(e: ApiError) -> Optional[int]:
        err = getattr(e, "error", None)
        try:
            if err is not None and err.is_incorrect_offset():
                return int(err.get_incorrect_offset().correct_offset)
        except Exception:
            return None
        return None
