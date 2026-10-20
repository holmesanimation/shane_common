"""M5-4B: bulk transfer on top of the verified Dropbox destination -- bounded concurrency, listing-based remote pre-check and verification, optional batch commit.

What changes versus ``DropboxDestination.store_file`` (one file at a time, ~3 API calls per file: metadata pre-check, upload, metadata verify):

* the remote pre-check of every small file is answered from ONE paginated recursive listing of the destination prefix (1 call per up to 2000 files);
* the post-upload verification of every small file is answered from a SECOND recursive listing (again ~1 call per 2000 files), taken after the uploads. A file that is absent
  from (or disagrees in) the listing is looked up with a targeted per-file metadata call before it is classified; if neither source establishes the expected Dropbox content
  hash the file stays UNVERIFIED and is reported. An incomplete listing never proves absence and never proves coverage;
* a bounded pool of workers hashes/uploads files concurrently, with a byte budget (backpressure) so memory stays bounded, a shared rate-limit gate, cooperative cancellation
  and the same per-file source-stability check (stat before hashing, after hashing, after verification);
* ``commit_mode="batch"`` sends small files with ``upload_session/start_batch`` + ``append_v2(close=True)`` + ``finish_batch_v2``. It does NOT reduce the request count versus
  ``files_upload`` (still ~1 data request per file); it moves the commit work server-side in groups, which is a throughput/lock-contention question that only a live
  benchmark can answer. It is therefore opt-in.

Independent verification rule is unchanged: a file is VERIFIED only when Dropbox itself (listing entry or metadata call, never the upload/commit response) reports a content
hash equal to the locally computed expected Dropbox content hash, and the source did not change. Nothing here deletes remote files or overwrites remote content that differs
from both the source and the caller's recorded evidence.
"""

from __future__ import annotations

import os
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from dropbox.exceptions import ApiError, DropboxException
from dropbox.files import CommitInfo, FileMetadata, FolderMetadata, UploadSessionCursor, UploadSessionFinishArg, WriteMode

from shane_common.runtime.transfer.dropbox_destination import (
    MIB, DropboxAuthInvalid, DropboxDestination, DropboxFailure, RemoteFile, UploadOutcome, UploadResult, _classify, hash_file_both,
)


# ------------------------------------------------------------------------------------------------ configuration
@dataclass(frozen=True)
class BulkConfig:
    workers: int = 6                              # evaluated range 4-8; the benchmark chooses the shipped default
    commit_mode: str = "single"                   # "single" (files_upload) | "batch" (session start_batch + finish_batch_v2)
    batch_size: int = 200                         # commit group size (Dropbox allows up to 1000)
    small_file_limit: int = 8 * MIB               # <= : listing pre-check + deferred listing verification; larger: DropboxDestination.store_file
    max_inflight_bytes: int = 256 * MIB           # backpressure: bytes held by in-flight workers
    verify_wave: int = 20_000                     # unverified small uploads are verified (listing) in waves of this many files
    use_listing: bool = True

    def __post_init__(self) -> None:
        if not 1 <= self.workers <= 32 or self.commit_mode not in ("single", "batch") or not 1 <= self.batch_size <= 1000 or self.verify_wave < 1:
            raise ValueError("invalid bulk configuration")


@dataclass(frozen=True)
class BulkItem:
    rel: str                                      # caller key (relative path inside the edge)
    local: Path
    remote_rel: str                               # destination-relative path (validated by DropboxDestination.remote_path)
    recorded_remote_hash: Optional[str] = None    # hash this caller last verified at the destination (permits an ``update(rev)`` refresh)


@dataclass
class BulkStats:
    files: int = 0
    verified: int = 0
    bytes_sent: int = 0
    list_calls: int = 0
    listed_entries: int = 0
    metadata_fallbacks: int = 0
    listing_complete: bool = True
    batch_commits: int = 0
    retries: int = 0
    rate_limits: int = 0
    peak_inflight_bytes: int = 0
    peak_inflight_files: int = 0
    seconds: float = 0.0
    verify_seconds: float = 0.0
    api_calls: dict = field(default_factory=dict)


class CountingClient:
    """Transparent proxy that counts SDK method calls by name (thread-safe). Used for the job history and for benchmarks."""

    def __init__(self, client: Any) -> None:
        object.__setattr__(self, "_client", client)
        object.__setattr__(self, "_counts", Counter())
        object.__setattr__(self, "_lock", threading.Lock())

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._client, name)
        if not callable(attr):
            return attr

        def call(*a: Any, **kw: Any) -> Any:
            with self._lock:
                self._counts[name] += 1
            return attr(*a, **kw)
        return call

    @property
    def counts(self) -> dict:
        with self._lock:
            return dict(self._counts)


# ------------------------------------------------------------------------------------------------ recursive listing
@dataclass
class RemoteListing:
    prefix: str
    entries: dict = field(default_factory=dict)       # lower-case path -> RemoteFile
    ambiguous: set = field(default_factory=set)       # lower-case paths seen twice with different content
    complete: bool = False
    pages: int = 0
    error: Optional[str] = None

    def get(self, remote_path: str) -> Optional[RemoteFile]:
        return self.entries.get(remote_path.lower())


def _is_not_found(e: ApiError) -> bool:
    err = getattr(e, "error", None)
    try:
        return bool(err is not None and err.is_path() and err.get_path().is_not_found())
    except Exception:                                  # noqa: BLE001
        return False


def list_remote_tree(dest: DropboxDestination, prefix_rel: str, *, should_cancel: Optional[Callable[[], bool]] = None) -> RemoteListing:
    """Recursive, paginated listing of ``prefix_rel`` (every page is retrieved). ``complete`` is True only when the last page reported ``has_more == False``. A prefix that does
    not exist yet is a COMPLETE empty listing (nothing is there). Any other failure leaves ``complete`` False, which callers must treat as 'proves nothing about absence'."""
    path = dest.remote_path(prefix_rel)
    out = RemoteListing(path)
    try:
        res, _ = dest._call(lambda: dest._c.files_list_folder(path, recursive=True, limit=2000, include_deleted=False))
        while True:
            out.pages += 1
            for md in res.entries:
                if isinstance(md, FileMetadata):
                    key = (md.path_lower or "").lower()
                    new = RemoteFile(md.path_display or key, int(md.size), getattr(md, "content_hash", None), getattr(md, "rev", None))
                    old = out.entries.get(key)
                    if old is not None and (old.content_hash != new.content_hash or old.size != new.size):
                        out.ambiguous.add(key)
                    out.entries[key] = new
            if not res.has_more:
                out.complete = True
                return out
            if should_cancel is not None and should_cancel():
                out.error = "cancelled"
                return out
            cursor = res.cursor
            res, _ = dest._call(lambda: dest._c.files_list_folder_continue(cursor))
    except DropboxAuthInvalid:
        raise
    except ApiError as e:
        if _is_not_found(e) and out.pages == 0:
            out.complete = True                        # the prefix does not exist: it holds nothing
        else:
            out.error = _classify(e).message
    except (DropboxException, OSError) as e:
        out.error = _classify(e).message
    return out


# ------------------------------------------------------------------------------------------------ uploader
@dataclass
class _Prep:
    item: BulkItem
    remote_path: str
    st: os.stat_result
    sha: str
    dbx: str
    size: int
    mode: Any = None
    refreshed: bool = False
    bytes_sent: int = 0
    attempts: int = 0
    session_id: Optional[str] = None


def _is_conflict(e: BaseException) -> bool:
    return isinstance(e, ApiError) and "conflict" in str(getattr(e, "error", e)).lower()


class BulkUploader:
    def __init__(self, dest: DropboxDestination, config: BulkConfig = BulkConfig()) -> None:
        self._d = dest
        self._cfg = config
        self.stats = BulkStats()
        self._lock = threading.Lock()
        self._cv = threading.Condition(self._lock)
        self._inflight_bytes = 0
        self._inflight_files = 0
        self._sessions: list = []
        self._sessions_left = 0
        self._prefix: Optional[str] = None

    # -- public ---------------------------------------------------------------------------------------------------------------------
    def store_many(self, items: Iterable[BulkItem], *, on_result: Callable[[BulkItem, UploadResult], None],
                   should_cancel: Optional[Callable[[], bool]] = None, list_prefix: Optional[str] = None,
                   on_uploaded: Optional[Callable[[BulkItem, int], None]] = None) -> BulkStats:
        """Upload/verify every item; ``on_result(item, result)`` is called ON THE CALLING THREAD exactly once per submitted item (verified or not). Items not submitted because of
        cancellation get no call (the caller treats them as not attempted)."""
        cfg, d = self._cfg, self._d
        t0 = time.monotonic()
        items = list(items)
        cancel = should_cancel or (lambda: False)
        collisions = Counter(d.remote_path(i.remote_rel).lower() for i in items)
        listing: Optional[RemoteListing] = None
        self._prefix = list_prefix
        if cfg.use_listing and items and list_prefix:
            listing = list_remote_tree(d, list_prefix, should_cancel=cancel)
            self.stats.list_calls += listing.pages
            self.stats.listed_entries += len(listing.entries)
            self.stats.listing_complete = listing.complete
        self._sessions_left = sum(1 for _ in items) if cfg.commit_mode == "batch" else 0
        pending: list = []
        buffered: list = []                                          # batch mode: appended sessions awaiting finish_batch
        done: dict = {}
        submitted = 0
        with ThreadPoolExecutor(max_workers=cfg.workers, thread_name_prefix="dbx-bulk") as pool:
            futs: dict = {}
            it = iter(items)
            exhausted = False
            while True:
                while not exhausted and len(futs) < cfg.workers * 2 and not cancel():
                    try:
                        item = next(it)
                    except StopIteration:
                        exhausted = True
                        break
                    if collisions[d.remote_path(item.remote_rel).lower()] > 1:
                        on_result(item, UploadResult(UploadOutcome.CONFLICT, d.remote_path(item.remote_rel),
                                                     failure=DropboxFailure("path", "two source files map to the same case-insensitive remote path", False)))
                        submitted += 1
                        continue
                    futs[pool.submit(self._work, item, listing, cancel)] = item
                    submitted += 1
                if not futs:
                    break
                fin, _ = wait(list(futs), return_when=FIRST_COMPLETED)
                for f in fin:
                    item = futs.pop(f)
                    r = f.result()                                    # DropboxAuthInvalid propagates (workers re-raise it)
                    if isinstance(r, _Prep):
                        if on_uploaded is not None:
                            on_uploaded(item, r.bytes_sent)
                        if cfg.commit_mode == "batch" and r.session_id is not None:
                            buffered.append(r)
                            if len(buffered) >= cfg.batch_size:
                                pending.extend(self._commit_batch(buffered, on_result))
                                buffered = []
                        else:
                            pending.append(r)
                    else:
                        self._emit(on_result, item, r)
                if len(pending) >= cfg.verify_wave:
                    self._verify_pending(pending, listing, on_result, cancel)
                    pending = []
                if exhausted and not futs:
                    break
                if cancel() and not futs:
                    break
        if buffered:
            pending.extend(self._commit_batch(buffered, on_result))
        self._verify_pending(pending, listing, on_result, cancel)
        self.stats.seconds = time.monotonic() - t0
        self.stats.retries, self.stats.rate_limits = d.stats["retries"], d.stats["rate_limits"]
        return self.stats

    # -- bookkeeping ----------------------------------------------------------------------------------------------------------------
    def _emit(self, on_result: Callable, item: BulkItem, r: UploadResult) -> None:
        self.stats.files += 1
        self.stats.bytes_sent += r.bytes_sent
        if r.verified:
            self.stats.verified += 1
        on_result(item, r)

    def _acquire(self, nbytes: int) -> int:
        need = min(max(nbytes, 1), self._cfg.max_inflight_bytes)
        with self._cv:
            while self._inflight_bytes + need > self._cfg.max_inflight_bytes and self._inflight_files > 0:
                self._cv.wait(0.5)
            self._inflight_bytes += need
            self._inflight_files += 1
            self.stats.peak_inflight_bytes = max(self.stats.peak_inflight_bytes, self._inflight_bytes)
            self.stats.peak_inflight_files = max(self.stats.peak_inflight_files, self._inflight_files)
        return need

    def _release(self, need: int) -> None:
        with self._cv:
            self._inflight_bytes -= need
            self._inflight_files -= 1
            self._cv.notify_all()

    # -- one file (worker thread) ---------------------------------------------------------------------------------------------------------
    def _work(self, item: BulkItem, listing: Optional[RemoteListing], cancel: Callable[[], bool]) -> Any:
        d = self._d
        remote_path = d.remote_path(item.remote_rel)
        if cancel():
            return UploadResult(UploadOutcome.CANCELLED, remote_path)
        try:
            size_hint = os.stat(item.local).st_size
        except OSError as e:
            return UploadResult(UploadOutcome.SOURCE_UNREADABLE, remote_path, failure=DropboxFailure("other", f"{type(e).__name__}: {e}"[:300], False))
        if size_hint > self._cfg.small_file_limit:
            need = self._acquire(2 * d._chunk if size_hint > d._single else size_hint)
            try:
                return d.store_file(item.local, item.remote_rel, recorded_remote_hash=item.recorded_remote_hash, should_cancel=cancel)
            finally:
                self._release(need)
        need = self._acquire(size_hint)
        try:
            return self._small(item, remote_path, listing, cancel)
        finally:
            self._release(need)

    def _small(self, item: BulkItem, remote_path: str, listing: Optional[RemoteListing], cancel: Callable[[], bool]) -> Any:
        d = self._d
        try:
            st1 = os.stat(item.local)
            sha, dbx, size = hash_file_both(item.local)
            st2 = os.stat(item.local)
        except OSError as e:
            return UploadResult(UploadOutcome.SOURCE_UNREADABLE, remote_path, failure=DropboxFailure("other", f"{type(e).__name__}: {e}"[:300], False))
        base = dict(remote_path=remote_path, size=size, sha256=sha, dropbox_hash=dbx, source_size=st1.st_size, source_mtime_ns=st1.st_mtime_ns)
        if (st1.st_size, st1.st_mtime_ns) != (st2.st_size, st2.st_mtime_ns) or size != st1.st_size:
            return UploadResult(UploadOutcome.SOURCE_CHANGED, **base)
        prep = _Prep(item, remote_path, st1, sha, dbx, size)
        try:
            remote = self._precheck(item, remote_path, listing)
            cls = self._classify_remote(prep, remote)
            if cls is not None:
                return UploadResult(cls[0], **base, remote_hash=cls[1], remote_rev=cls[2], failure=cls[3])
            if cancel():
                return UploadResult(UploadOutcome.CANCELLED, **base)
            data = item.local.read_bytes()
            if self._cfg.commit_mode == "batch":
                sid = self._take_session()
                _, a = d._call(lambda: d._c.files_upload_session_append_v2(data, UploadSessionCursor(sid, 0), close=True))
                prep.session_id, prep.attempts, prep.bytes_sent = sid, a, len(data)
                return prep
            try:
                _, a = d._call(lambda: d._c.files_upload(data, remote_path, mode=prep.mode, mute=True))
            except ApiError as e:
                if not _is_conflict(e):
                    raise
                remote = d.get_remote(item.remote_rel)                    # the listing lagged: decide from authoritative metadata, never overwrite blindly
                cls = self._classify_remote(prep, remote, after_conflict=True)
                if cls is None:
                    raise
                return UploadResult(cls[0], **base, remote_hash=cls[1], remote_rev=cls[2], failure=cls[3])
            prep.attempts, prep.bytes_sent = a, len(data)
            return prep
        except DropboxAuthInvalid:
            raise
        except OSError as e:
            if isinstance(e, (FileNotFoundError, PermissionError)):
                return UploadResult(UploadOutcome.SOURCE_UNREADABLE, **base, failure=DropboxFailure("other", f"{type(e).__name__}: {e}"[:300], False))
            return UploadResult(UploadOutcome.FAILED, **base, failure=_classify(e))
        except (DropboxException, ConnectionError, TimeoutError) as e:
            return UploadResult(UploadOutcome.FAILED, **base, failure=_classify(e))

    def _precheck(self, item: BulkItem, remote_path: str, listing: Optional[RemoteListing]) -> Optional[RemoteFile]:
        """Remote state before uploading. A positive listing hit is used as is; 'absent from the listing' is trusted only when the listing is COMPLETE. Otherwise the targeted
        metadata call decides (never 'absent' by inference)."""
        if listing is not None:
            key = remote_path.lower()
            if key in listing.ambiguous:
                return RemoteFile(remote_path, -2, None, None)           # sentinel: ambiguous listing entry
            hit = listing.get(remote_path)
            if hit is not None:
                return hit
            if listing.complete:
                return None
        self.stats.metadata_fallbacks += 1
        return self._d.get_remote(item.remote_rel)

    def _classify_remote(self, prep: _Prep, remote: Optional[RemoteFile], after_conflict: bool = False) -> Optional[tuple]:
        """None -> proceed to upload with ``prep.mode`` set; else (outcome, remote_hash, remote_rev, failure)."""
        item = prep.item
        if remote is not None and remote.size == -2:
            return (UploadOutcome.CONFLICT, None, None, DropboxFailure("path", "ambiguous remote listing entry for this path", False))
        if remote is not None and remote.size == -1:
            return (UploadOutcome.CONFLICT, None, None, DropboxFailure("path", "a folder exists at the file path", False))
        if remote is None:
            if after_conflict:
                return None
            prep.mode = WriteMode.add
            return None
        if remote.content_hash == prep.dbx:
            return (UploadOutcome.ALREADY_PRESENT_VERIFIED, remote.content_hash, remote.rev, None)
        if item.recorded_remote_hash is not None and remote.content_hash == item.recorded_remote_hash and remote.rev:
            prep.mode, prep.refreshed = WriteMode.update(remote.rev), True
            return None
        return (UploadOutcome.CONFLICT, remote.content_hash, remote.rev, None)

    # -- sessions / batch commit ---------------------------------------------------------------------------------------------------
    def _take_session(self) -> str:
        d = self._d
        with self._lock:
            if not self._sessions:
                n = max(1, min(self._cfg.batch_size, self._sessions_left))
                res, _ = d._call(lambda: d._c.files_upload_session_start_batch(n))
                self._sessions.extend(res.session_ids)
            self._sessions_left -= 1
            return self._sessions.pop()

    def _commit_batch(self, batch: list, on_result: Callable) -> list:
        """Commit appended sessions in one call. Returns the entries that now await independent verification; failures are reported here."""
        d = self._d
        ok: list = []
        todo = list(batch)
        for round_no in range(2):
            if not todo:
                break
            entries = [UploadSessionFinishArg(UploadSessionCursor(p.session_id, p.size), CommitInfo(p.remote_path, mode=p.mode, mute=True)) for p in todo]
            try:
                res, _ = d._call(lambda: d._c.files_upload_session_finish_batch_v2(entries))
            except DropboxAuthInvalid:
                raise
            except (DropboxException, OSError) as e:
                for p in todo:
                    self._emit(on_result, p.item, self._res(p, UploadOutcome.FAILED, failure=_classify(e)))
                return ok
            self.stats.batch_commits += 1
            retry: list = []
            for p, ent in zip(todo, res.entries):
                if ent.is_success():
                    ok.append(p)
                    continue
                err = ent.get_failure()
                text = str(err)
                if round_no == 0 and "too_many_write_operations" in text:
                    retry.append(p)
                elif "conflict" in text:
                    remote = d.get_remote(p.item.remote_rel)
                    cls = self._classify_remote(p, remote, after_conflict=True)
                    if cls is None:
                        self._emit(on_result, p.item, self._res(p, UploadOutcome.FAILED, failure=DropboxFailure("api", text[:300], False)))
                    else:
                        self._emit(on_result, p.item, self._res(p, cls[0], remote_hash=cls[1], remote_rev=cls[2], failure=cls[3]))
                else:
                    self._emit(on_result, p.item, self._res(p, UploadOutcome.FAILED, failure=DropboxFailure("api", text[:300], round_no == 0)))
            todo = retry
        for p in todo:                                                    # still contended after the retry round
            self._emit(on_result, p.item, self._res(p, UploadOutcome.FAILED, failure=DropboxFailure("rate_limit", "too_many_write_operations", True)))
        return ok

    # -- independent verification ----------------------------------------------------------------------------------------------------
    def _res(self, p: _Prep, outcome: UploadOutcome, **kw: Any) -> UploadResult:
        return UploadResult(outcome, p.remote_path, size=p.size, sha256=p.sha, dropbox_hash=p.dbx, source_size=p.st.st_size, source_mtime_ns=p.st.st_mtime_ns,
                            bytes_sent=p.bytes_sent, attempts=p.attempts, **kw)

    def _verify_pending(self, pending: list, listing: Optional[RemoteListing], on_result: Callable, cancel: Callable[[], bool]) -> None:
        if not pending:
            return
        t0 = time.monotonic()
        d = self._d
        if cancel():
            for p in pending:                                           # uploaded but not independently verified: nothing is claimed; the next run finds them present
                self._emit(on_result, p.item, self._res(p, UploadOutcome.CANCELLED))
            return
        fresh: Optional[RemoteListing] = None
        if self._cfg.use_listing and self._prefix:
            fresh = list_remote_tree(d, self._prefix, should_cancel=cancel)
            self.stats.list_calls += fresh.pages
            self.stats.listed_entries += len(fresh.entries)
            self.stats.listing_complete = self.stats.listing_complete and fresh.complete
        for p in pending:
            if cancel():                                                  # stop between files; everything not yet verified stays unclaimed (the next run finds it present)
                self._emit(on_result, p.item, self._res(p, UploadOutcome.CANCELLED))
                continue
            remote: Optional[RemoteFile] = None
            ambiguous = False
            if fresh is not None:
                ambiguous = p.remote_path.lower() in fresh.ambiguous
                remote = fresh.get(p.remote_path)
            if ambiguous or remote is None or remote.content_hash != p.dbx:       # absent or disagreeing: authoritative targeted lookup before classifying
                self.stats.metadata_fallbacks += 1
                try:
                    remote = d.get_remote(p.item.remote_rel)
                except DropboxAuthInvalid:
                    raise
                except (DropboxException, OSError) as e:
                    self._emit(on_result, p.item, self._res(p, UploadOutcome.UNVERIFIED, failure=_classify(e)))
                    continue
            try:
                st3 = os.stat(p.item.local)
            except OSError:
                self._emit(on_result, p.item, self._res(p, UploadOutcome.SOURCE_CHANGED, remote_hash=remote.content_hash if remote else None))
                continue
            rh = remote.content_hash if remote is not None and remote.size >= 0 else None
            rrev = remote.rev if remote is not None else None
            if (st3.st_size, st3.st_mtime_ns) != (p.st.st_size, p.st.st_mtime_ns):
                self._emit(on_result, p.item, self._res(p, UploadOutcome.SOURCE_CHANGED, remote_hash=rh, remote_rev=rrev))
            elif remote is None:
                self._emit(on_result, p.item, self._res(p, UploadOutcome.UNVERIFIED, failure=DropboxFailure("api", "absent from listing and metadata after upload", True)))
            elif rh == p.dbx:
                self._emit(on_result, p.item, self._res(p, UploadOutcome.REFRESHED_VERIFIED if p.refreshed else UploadOutcome.VERIFIED_UPLOADED, remote_hash=rh, remote_rev=rrev))
            else:
                self._emit(on_result, p.item, self._res(p, UploadOutcome.HASH_MISMATCH, remote_hash=rh, remote_rev=rrev))
        self.stats.verify_seconds += time.monotonic() - t0
