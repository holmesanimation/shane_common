"""In-memory Dropbox SDK double for fixture tests (no network, no credentials). Behaves like the subset of ``dropbox.Dropbox`` used by ``DropboxDestination`` and the bulk
uploader (``dropbox_bulk``). Thread-safe, so concurrent-worker tests and benchmarks can share one instance.

Fault injection: ``fail[method] = [exc, ...]`` raises each exception once, in order; ``corrupt_next_upload`` makes the stored bytes differ from what was sent (so the remote
content hash differs); ``on_upload`` is called with (path) just before an upload is stored (e.g. to modify the source mid-upload); ``forget_sessions()`` drops all upload sessions;
``hide_from_listing`` is a set of lower-case paths that exist (metadata lookups see them) but do not appear in recursive listings yet (listing-consistency lag);
``list_page_size`` caps entries per listing page; ``latency`` adds a per-call sleep (benchmark realism); ``calls`` / ``call_counts`` record every API call by name.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Optional

from dropbox.exceptions import ApiError
from dropbox.files import (
    FileMetadata, FolderMetadata, GetMetadataError, ListFolderError, ListFolderResult, LookupError as DbxLookupError, UploadSessionAppendError,
    UploadSessionFinishBatchResult, UploadSessionFinishBatchResultEntry, UploadSessionFinishError, UploadSessionLookupError, UploadSessionOffsetError,
    UploadSessionStartBatchResult, UploadSessionStartResult, WriteConflictError, WriteError,
)

import hashlib

from shane_common.runtime.transfer.dropbox_destination import BLOCK, dropbox_content_hash_stream


def _hash(b: bytes) -> str:
    pos = [0]

    def read(n: int) -> bytes:
        c = b[pos[0]:pos[0] + n]
        pos[0] += len(c)
        return c
    return dropbox_content_hash_stream(read)


class _Blob:
    """Stored object when ``retain_data=False`` (benchmarks): only its size and Dropbox content hash, so the double does not add the file bytes to the measured process memory."""

    __slots__ = ("size", "dropbox_hash")

    def __init__(self, size: int, dropbox_hash: str) -> None:
        self.size, self.dropbox_hash = size, dropbox_hash

    def __len__(self) -> int:
        return self.size


class _Stream:
    """Upload-session buffer that keeps only running block hashes (no data)."""

    def __init__(self) -> None:
        self.blocks, self.tail, self.size = bytearray(), bytearray(), 0

    def __len__(self) -> int:
        return self.size

    def extend(self, data: Any) -> None:
        mv = memoryview(data)
        self.size += len(mv)
        pos = 0
        if self.tail:
            take = min(BLOCK - len(self.tail), len(mv))
            self.tail.extend(mv[:take])
            pos = take
            if len(self.tail) == BLOCK:
                self.blocks.extend(hashlib.sha256(bytes(self.tail)).digest())
                self.tail.clear()
        while len(mv) - pos >= BLOCK:
            self.blocks.extend(hashlib.sha256(mv[pos:pos + BLOCK]).digest())
            pos += BLOCK
        self.tail.extend(mv[pos:])

    def finish(self) -> _Blob:
        blocks = bytearray(self.blocks)
        if self.tail:
            blocks.extend(hashlib.sha256(bytes(self.tail)).digest())
        return _Blob(self.size, hashlib.sha256(bytes(blocks)).hexdigest())


def not_found_error() -> ApiError:
    return ApiError("req", GetMetadataError.path(DbxLookupError.not_found), "not_found", "en")


def list_not_found_error() -> ApiError:
    return ApiError("req", ListFolderError.path(DbxLookupError.not_found), "not_found", "en")


class FakeDropbox:
    def __init__(self, *, latency: float = 0.0, list_page_size: int = 2000, link_bytes_per_second: float = 0.0, retain_data: bool = True) -> None:
        self.files: dict[str, tuple[bytes, str]] = {}
        self.display: dict[str, str] = {}
        self.folders: set = set()
        self.sessions: dict[str, bytearray] = {}
        self.calls: list = []
        self.call_counts: Counter = Counter()
        self.fail: dict[str, list] = {}
        self.corrupt_next_upload = False
        self.on_upload: Optional[Callable[[str], None]] = None
        self.hide_from_listing: set = set()
        self.history: dict = {}                                           # path -> [rev, ...] every write ever made (add/update/overwrite)
        self.atomic_add = False                                           # TEST ONLY: check+write of an add-mode upload is one atomic step (a server WITH create-if-absent)
        self.honour_range = True                                          # TEST ONLY: False models a server that ignores Range (answers 200 with the whole body)
        self.lax_add = False                                              # TEST ONLY: add-mode silently overwrites, i.e. a server WITHOUT atomic create-if-absent
        self.list_page_size = list_page_size
        self.latency = latency
        self.retain_data = retain_data                                    # False: keep only size + hash of stored objects (no downloads possible); for memory benchmarks
        self.link_bytes_per_second = link_bytes_per_second               # shared uplink model: uploads of all workers serialize through it (0 = unlimited)
        self._link_lock = threading.Lock()
        self.listings: dict[str, tuple] = {}
        self._rev = 0
        self._sid = 0
        self._lock = threading.RLock()

    # -- helpers -------------------------------------------------------------------------------------------------------------------
    def _maybe_fail(self, method: str) -> None:
        with self._lock:
            self.calls.append(method)
            self.call_counts[method] += 1
            q = self.fail.get(method)
            exc = q.pop(0) if q else None
        if self.latency:
            time.sleep(self.latency)
        if exc is not None:
            raise exc

    def _store(self, path: str, data: bytes) -> FileMetadata:
        if self.on_upload:
            self.on_upload(path)
        if not self.retain_data and not isinstance(data, _Blob):
            data = _Stream_of(data)
        with self._lock:
            if self.corrupt_next_upload and self.retain_data:
                self.corrupt_next_upload = False
                data = data[:-1] + bytes([data[-1] ^ 0xFF]) if data else b"\x00"
            self._rev += 1
            rev = f"{self._rev:08x}a"
            self.files[path.lower()] = (data, rev)
            self.history.setdefault(path.lower(), []).append(rev)
            self.display[path.lower()] = path
            return self._md(path)

    def _md(self, path: str) -> FileMetadata:
        data, rev = self.files[path.lower()]
        now = dt.datetime(2026, 1, 1)
        return FileMetadata(name=path.rsplit("/", 1)[-1], id="id:" + rev, client_modified=now, server_modified=now, rev=rev, size=len(data), path_lower=path.lower(),
                            path_display=self.display.get(path.lower(), path), content_hash=data.dropbox_hash if isinstance(data, _Blob) else _hash(data))

    def _link(self, nbytes: int) -> None:
        if self.link_bytes_per_second and nbytes:
            with self._link_lock:
                time.sleep(nbytes / self.link_bytes_per_second)

    # -- API subset ------------------------------------------------------------------------------------------------------------------
    def files_get_metadata(self, path: str, **kw: Any):
        self._maybe_fail("files_get_metadata")
        with self._lock:
            if path.lower() in self.files:
                return self._md(path)
            if path.lower() in self.folders:
                return FolderMetadata(name=path.rsplit("/", 1)[-1], id="id:f", path_lower=path.lower(), path_display=path)
        raise not_found_error()

    def files_list_revisions(self, path: str, limit: int = 10, **kw: Any):
        self._maybe_fail("files_list_revisions")
        with self._lock:
            revs = list(reversed(self.history.get(path.lower(), [])))[:limit]
        if not revs:
            raise not_found_error()
        import types
        return types.SimpleNamespace(entries=[types.SimpleNamespace(rev=r) for r in revs], is_deleted=False)

    def files_upload(self, data: bytes, path: str, mode: Any = None, mute: bool = False, **kw: Any):
        self._maybe_fail("files_upload")
        self._link(len(data))
        if self.atomic_add:
            with self._lock:
                self._check_mode(path, mode)
                return self._store(path, bytes(data))
        self._check_mode(path, mode)
        return self._store(path, bytes(data))

    def download_range_raw(self, path: str, start: int, end: int):
        """(status, headers, body) like a raw HTTP Range request against files/download."""
        self._maybe_fail("download_range_raw")
        with self._lock:
            if path.lower() not in self.files:
                raise not_found_error()
            data = self.files[path.lower()][0]
        if not isinstance(data, (bytes, bytearray)):
            raise RuntimeError("range reads need retain_data=True")
        if not self.honour_range:
            return 200, {"Content-Length": str(len(data))}, bytes(data)
        return 206, {"Content-Range": f"bytes {start}-{end}/{len(data)}"}, bytes(data[start:end + 1])

    def _check_mode(self, path: str, mode: Any) -> None:
        with self._lock:
            exists = path.lower() in self.files
            if path.lower() in self.folders:
                raise ApiError("req", "path/conflict/folder", "conflict", "en")
            if mode is not None and mode.is_add() and exists and not self.lax_add:
                raise ApiError("req", "path/conflict/file", "conflict", "en")
            if mode is not None and mode.is_update() and (not exists or self.files[path.lower()][1] != mode.get_update()):
                raise ApiError("req", "path/conflict/file", "conflict", "en")

    def _new_buf(self, data: bytes) -> Any:
        if self.retain_data:
            return bytearray(data)
        s = _Stream()
        s.extend(data)
        return s

    @staticmethod
    def _final(buf: Any) -> Any:
        return buf.finish() if isinstance(buf, _Stream) else bytes(buf)

    def files_upload_session_start(self, data: bytes, **kw: Any):
        self._maybe_fail("files_upload_session_start")
        self._link(len(data))
        with self._lock:
            self._sid += 1
            sid = f"sess{self._sid}"
            self.sessions[sid] = self._new_buf(data)
        return UploadSessionStartResult(sid)

    def files_upload_session_start_batch(self, num_sessions: int, session_type: Any = None, **kw: Any):
        self._maybe_fail("files_upload_session_start_batch")
        if not 1 <= num_sessions <= 1000:
            raise ApiError("req", "num_sessions out of range", "bad", "en")
        with self._lock:
            ids = []
            for _ in range(num_sessions):
                self._sid += 1
                sid = f"sess{self._sid}"
                self.sessions[sid] = self._new_buf(b"")
                ids.append(sid)
        return UploadSessionStartBatchResult(ids)

    def files_upload_session_append_v2(self, data: bytes, cursor: Any, close: bool = False, **kw: Any):
        self._maybe_fail("files_upload_session_append_v2")
        self._link(len(data))
        with self._lock:
            if cursor.session_id not in self.sessions:
                raise ApiError("req", UploadSessionAppendError.not_found, "gone", "en")
            buf = self.sessions[cursor.session_id]
            if cursor.offset != len(buf):
                raise ApiError("req", UploadSessionAppendError.incorrect_offset(UploadSessionOffsetError(len(buf))), "offset", "en")
            buf.extend(data)

    def files_upload_session_finish(self, data: bytes, cursor: Any, commit: Any, **kw: Any):
        self._maybe_fail("files_upload_session_finish")
        self._link(len(data))
        with self._lock:
            if cursor.session_id not in self.sessions:
                raise ApiError("req", UploadSessionLookupError.not_found, "gone", "en")
            buf = self.sessions[cursor.session_id]
            if cursor.offset != len(buf):
                raise ApiError("req", UploadSessionLookupError.incorrect_offset(UploadSessionOffsetError(len(buf))), "offset", "en")
            self._check_mode(commit.path, commit.mode)
            buf.extend(data)
            del self.sessions[cursor.session_id]
        return self._store(commit.path, self._final(buf))

    def files_upload_session_finish_batch_v2(self, entries: list, **kw: Any):
        self._maybe_fail("files_upload_session_finish_batch_v2")
        if not 1 <= len(entries) <= 1000:
            raise ApiError("req", "entries out of range", "bad", "en")
        out = []
        for e in entries:
            try:
                with self._lock:
                    sid = e.cursor.session_id
                    if sid not in self.sessions:
                        raise ApiError("req", UploadSessionFinishError.lookup_failed(UploadSessionLookupError.not_found), "gone", "en")
                    buf = self.sessions[sid]
                    if e.cursor.offset != len(buf):
                        raise ApiError("req", UploadSessionFinishError.lookup_failed(UploadSessionLookupError.incorrect_offset(UploadSessionOffsetError(len(buf)))), "offset", "en")
                    self._check_mode(e.commit.path, e.commit.mode)
                    del self.sessions[sid]
                out.append(UploadSessionFinishBatchResultEntry.success(self._store(e.commit.path, self._final(buf))))
            except ApiError as exc:
                err = exc.error if isinstance(exc.error, UploadSessionFinishError) else (UploadSessionFinishError.path(WriteError.conflict(WriteConflictError.file)) if "conflict" in str(exc.error) else UploadSessionFinishError.other)
                out.append(UploadSessionFinishBatchResultEntry.failure(err))
        return UploadSessionFinishBatchResult(out)

    def files_download_to_file(self, download_path: str, path: str, **kw: Any):
        self._maybe_fail("files_download_to_file")
        with self._lock:
            if path.lower() not in self.files:
                raise not_found_error()
            data = self.files[path.lower()][0]
            md = self._md(path)
        Path(download_path).write_bytes(data)
        return md

    def files_delete_v2(self, path: str, **kw: Any):                      # for test setup only; the destination never deletes
        with self._lock:
            self.files.pop(path.lower(), None)

    # -- listing -----------------------------------------------------------------------------------------------------------------------
    def files_list_folder(self, path: str, recursive: bool = False, limit: Optional[int] = None, **kw: Any):
        self._maybe_fail("files_list_folder")
        prefix = path.lower().rstrip("/")
        with self._lock:
            if prefix and not any(p.startswith(prefix + "/") for p in self.files) and prefix not in self.folders:
                raise list_not_found_error()
            keys = sorted(p for p in self.files if (not prefix or p.startswith(prefix + "/")) and p not in self.hide_from_listing
                          and (recursive or "/" not in p[len(prefix) + 1:]))
            snapshot = [self._md(self.display[p]) for p in keys]
        page = min(limit or self.list_page_size, self.list_page_size)
        return self._page(snapshot, 0, page)

    def files_list_folder_continue(self, cursor: str, **kw: Any):
        self._maybe_fail("files_list_folder_continue")
        with self._lock:
            snap, page = self.listings[cursor]
            start = int(cursor.rsplit(":", 1)[1])
        return self._page(snap, start, page)

    def _page(self, snapshot: list, start: int, page: int) -> ListFolderResult:
        end = start + page
        more = end < len(snapshot)
        with self._lock:
            cur = f"cur{len(self.listings)}:{end}"
            self.listings[cur] = (snapshot, page)
        return ListFolderResult(entries=snapshot[start:end], cursor=cur, has_more=more)

    def forget_sessions(self) -> None:
        with self._lock:
            self.sessions.clear()


def _Stream_of(data: bytes) -> _Blob:
    s = _Stream()
    s.extend(data)
    return s.finish()
