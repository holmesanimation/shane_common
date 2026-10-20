"""Application-neutral object primitives over ``DropboxDestination``: in-memory add-only stores, strict commit-marker creation, verified reads and range reads.

Nothing here deletes, renames or overwrites. Every write uses ``WriteMode.add`` with ``autorename=False`` (a path that exists is never replaced and never silently renamed).

Marker semantics are deliberately conservative and **unverified against the live service**: whether Dropbox refuses a second ``add`` to an existing path atomically under a race is
exactly what the separately authorized live scratch test must establish (see ``MARKER_LIVE_TEST``). ``store_marker`` therefore never claims more than it observed:
  WON               our bytes were accepted, Dropbox independently reports our content hash, and (when the client can tell) the path has exactly one server revision
  EXISTS_SAME       the path already holds exactly our bytes (also the honest result of an automatic retry after a lost response)
  EXISTS_DIFFERENT  the path holds different content (or a folder): another writer; nothing was written
  RACE_SUSPECTED    our bytes are present but the path has more than one server revision: a concurrent/overwriting writer; the caller must NOT claim the commit
  HASH_MISMATCH     the upload was accepted but Dropbox reports different content: not verified
A transport failure whose outcome cannot be established raises ``MarkerUnknown``; it is never reported as a conflict or a success.
"""

from __future__ import annotations

import enum
import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from dropbox.exceptions import ApiError, DropboxException
from dropbox.files import FileMetadata, FolderMetadata, WriteMode

from .dropbox_destination import DropboxAuthInvalid, DropboxDestination, dropbox_content_hash_stream

MAX_MARKER_BYTES = 1024 * 1024

MARKER_LIVE_TEST = (
    "Bounded live scratch-namespace test (separately authorized, NOT run): in a throw-away folder under the app folder, (1) add-mode upload of path P from N=16 threads/2 processes "
    "with different payloads and check exactly one WON and N-1 conflicts with a single server revision; (2) repeat with identical payloads; (3) upload, then re-add the same path "
    "and inspect files_list_revisions; (4) kill a client mid-request and read back; (5) measure whether list_folder shows the object immediately; then delete nothing and record results."
)


class MarkerOutcome(str, enum.Enum):
    WON = "WON"
    EXISTS_SAME = "EXISTS_SAME"
    EXISTS_DIFFERENT = "EXISTS_DIFFERENT"
    RACE_SUSPECTED = "RACE_SUSPECTED"
    HASH_MISMATCH = "HASH_MISMATCH"


class MarkerUnknown(RuntimeError):
    """The marker request failed in a way that does not establish whether Dropbox stored it."""


class RangeNotHonoured(RuntimeError):
    pass


class RemoteIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class MarkerResult:
    outcome: MarkerOutcome
    remote_hash: Optional[str] = None
    rev: Optional[str] = None
    revisions: Optional[int] = None      # None = the client cannot report revisions (never guessed)


@dataclass(frozen=True)
class BytesResult:
    state: str                           # stored | present_same | conflict | hash_mismatch
    remote_hash: Optional[str] = None


def content_hash_of(data: bytes) -> str:
    pos = 0

    def read(n: int) -> bytes:
        nonlocal pos
        chunk = data[pos:pos + n]
        pos += len(chunk)
        return chunk

    return dropbox_content_hash_stream(read)


def is_conflict(e: ApiError) -> bool:
    """True only for a write conflict (path already exists / is a folder). Understands the SDK's error objects; anything else is NOT a conflict."""
    err = getattr(e, "error", None)
    if err is None:
        return False
    if isinstance(err, str):                                              # test doubles
        return "conflict" in err
    try:
        if err.is_path():
            reason = err.get_path().reason
            return bool(reason.is_conflict())
    except Exception:
        return False
    return False


def _metadata(dest: DropboxDestination, path: str) -> Optional[Any]:
    try:
        md, _ = dest._call(lambda: dest._c.files_get_metadata(path))
        return md
    except ApiError as e:
        err = getattr(e, "error", None)
        if err is not None and hasattr(err, "is_path") and err.is_path() and err.get_path().is_not_found():
            return None
        raise


def _revisions(dest: DropboxDestination, path: str) -> Optional[int]:
    fn = getattr(dest._c, "files_list_revisions", None)
    if fn is None:
        return None
    try:
        return len(fn(path, limit=10).entries)
    except DropboxAuthInvalid:
        raise
    except (ApiError, DropboxException, OSError) as e:
        raise MarkerUnknown(f"revision lookup failed: {type(e).__name__}") from e


# ------------------------------------------------------------------------------------------------------------------ markers
def store_marker(dest: DropboxDestination, data: bytes, rel: str) -> MarkerResult:
    if not isinstance(data, (bytes, bytearray)) or not 0 < len(data) <= MAX_MARKER_BYTES:
        raise ValueError("marker payload must be 1 byte .. 1 MiB")
    path = dest.remote_path(rel)
    want = content_hash_of(bytes(data))
    try:
        dest._call(lambda: dest._c.files_upload(bytes(data), path, mode=WriteMode.add, autorename=False, mute=True))
    except ApiError as e:
        if not is_conflict(e):
            raise MarkerUnknown(f"marker upload failed ({type(e).__name__}); outcome not established") from e
        return _classify_existing(dest, path, want)
    except DropboxAuthInvalid:
        raise
    except (DropboxException, OSError) as e:
        # The request may or may not have been applied: only the server's state can tell, and then only honestly.
        try:
            md = _metadata(dest, path)
        except (ApiError, DropboxException, OSError) as e2:
            raise MarkerUnknown(f"marker upload failed ({type(e).__name__}) and the read-back failed ({type(e2).__name__})") from e
        if md is None:
            raise MarkerUnknown(f"marker upload failed ({type(e).__name__}); the path is absent") from e
        return _classify_existing(dest, path, want, md)
    md = _metadata(dest, path)                                           # independent verification, never the upload response
    if md is None or isinstance(md, FolderMetadata):
        raise MarkerUnknown("marker accepted but the path cannot be read back")
    remote_hash, rev = getattr(md, "content_hash", None), getattr(md, "rev", None)
    if remote_hash != want:
        return MarkerResult(MarkerOutcome.HASH_MISMATCH, remote_hash, rev)
    n = _revisions(dest, path)
    if n is not None and n != 1:
        return MarkerResult(MarkerOutcome.RACE_SUSPECTED, remote_hash, rev, n)
    return MarkerResult(MarkerOutcome.WON, remote_hash, rev, n)


def _classify_existing(dest: DropboxDestination, path: str, want: str, md: Optional[Any] = None) -> MarkerResult:
    try:
        md = md or _metadata(dest, path)
    except (ApiError, DropboxException, OSError) as e:
        raise MarkerUnknown(f"marker conflict reported but the existing object cannot be read: {type(e).__name__}") from e
    if md is None:
        raise MarkerUnknown("marker conflict reported but the path is absent")
    if isinstance(md, FolderMetadata):
        return MarkerResult(MarkerOutcome.EXISTS_DIFFERENT)
    h = getattr(md, "content_hash", None)
    return MarkerResult(MarkerOutcome.EXISTS_SAME if h == want else MarkerOutcome.EXISTS_DIFFERENT, h, getattr(md, "rev", None))


# ------------------------------------------------------------------------------------------------------------------ add-only bytes
def store_bytes(dest: DropboxDestination, data: bytes, rel: str, *, spill_dir: Optional[Path] = None) -> BytesResult:
    """Add-only, hash-verified store of an in-memory object (content-addressed objects). Existing identical bytes -> present_same; existing different -> conflict (never replaced)."""
    path = dest.remote_path(rel)
    want = content_hash_of(bytes(data))
    if len(data) > dest._single:
        if spill_dir is None:
            raise ValueError("objects above the single-shot limit need a spill_dir")
        Path(spill_dir).mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=spill_dir, delete=False) as fh:
            fh.write(data)
            name = fh.name
        try:
            r = dest.store_file(name, rel, allow_refresh=False)
        finally:
            Path(name).unlink(missing_ok=True)
        from .dropbox_destination import UploadOutcome
        if r.outcome is UploadOutcome.CONFLICT:
            return BytesResult("conflict", r.remote_hash)
        if r.verified:
            return BytesResult("present_same" if r.outcome is UploadOutcome.ALREADY_PRESENT_VERIFIED else "stored", r.remote_hash)
        return BytesResult("hash_mismatch", r.remote_hash)
    md = _metadata(dest, path)
    if md is not None:
        if isinstance(md, FolderMetadata):
            return BytesResult("conflict")
        h = getattr(md, "content_hash", None)
        return BytesResult("present_same" if h == want else "conflict", h)
    try:
        dest._call(lambda: dest._c.files_upload(bytes(data), path, mode=WriteMode.add, autorename=False, mute=True))
    except ApiError as e:
        if not is_conflict(e):
            raise
        md = _metadata(dest, path)                                        # lost the creation race: whoever is there decides
        h = getattr(md, "content_hash", None) if md is not None and not isinstance(md, FolderMetadata) else None
        return BytesResult("present_same" if h == want else "conflict", h)
    md = _metadata(dest, path)
    h = getattr(md, "content_hash", None) if md is not None else None
    return BytesResult("stored" if h == want else "hash_mismatch", h)


def read_verified_bytes(dest: DropboxDestination, rel: str, *, spill_dir: Path, max_bytes: int = 2 * 1024 ** 3) -> Optional[bytes]:
    """None when absent. The downloaded bytes must match Dropbox's independently reported content hash, else RemoteIntegrityError."""
    path = dest.remote_path(rel)
    md = _metadata(dest, path)
    if md is None:
        return None
    if isinstance(md, FolderMetadata):
        raise RemoteIntegrityError(f"{rel} is a folder")
    if md.size > max_bytes:
        raise RemoteIntegrityError(f"{rel} is larger than the {max_bytes} byte read limit")
    Path(spill_dir).mkdir(parents=True, exist_ok=True)
    tmp = Path(spill_dir) / ("rv_" + hashlib.sha256(rel.encode()).hexdigest()[:16])
    try:
        d = dest.download_to(rel, tmp)
        if not d.remote_hash_ok:
            raise RemoteIntegrityError(f"Dropbox hash disagrees with the downloaded bytes: {rel}")
        return tmp.read_bytes()
    finally:
        tmp.unlink(missing_ok=True)


# ------------------------------------------------------------------------------------------------------------------ range reads
def sdk_range_request(client: Any, path: str, start: int, end: int) -> tuple:
    """Raw HTTP Range request against ``files/download`` for the real SDK client -> (status, headers, body). UNVERIFIED in production use (Phase 1 observed 206 once); relies on the SDK's
    access-token attribute, which is private API. The token is only placed in the Authorization header and is never returned or logged."""
    import requests
    try:
        client.check_and_refresh_access_token()
    except Exception:
        pass
    token = getattr(client, "_oauth2_access_token", None)
    if not token:
        raise RangeNotHonoured("the client exposes no access token for a raw range request")
    r = requests.post("https://content.dropboxapi.com/2/files/download",
                      headers={"Authorization": "Bearer " + token, "Dropbox-API-Arg": json.dumps({"path": path}), "Range": f"bytes={start}-{end}"}, timeout=120)
    return r.status_code, dict(r.headers), r.content


def download_range(dest: DropboxDestination, rel: str, start: int, length: int) -> bytes:
    """Bytes [start, start+length). Fails closed: anything but a 206 with the exact requested Content-Range and length raises ``RangeNotHonoured`` (the caller falls back to a whole
    download). A range read has no content hash of its own: the caller MUST verify what it extracts (ZIP CRC + the manifest's per-member SHA-256)."""
    if start < 0 or length <= 0:
        raise ValueError("start must be >= 0 and length > 0")
    path = dest.remote_path(rel)
    md = _metadata(dest, path)
    if md is None or isinstance(md, FolderMetadata):
        raise RangeNotHonoured(f"{rel} is not a file")
    size = int(md.size)
    if start >= size:
        raise ValueError("range starts beyond the end of the object")
    end = min(start + length, size) - 1
    fn = getattr(dest._c, "download_range_raw", None)
    if fn is not None:
        (status, headers, body), _ = dest._call(lambda: fn(path, start, end))
    else:
        status, headers, body = sdk_range_request(dest._c, path, start, end)
    if status != 206:
        raise RangeNotHonoured(f"server answered {status}, not 206 Partial Content")
    expect = f"bytes {start}-{end}/{size}"
    got = {k.lower(): v for k, v in headers.items()}.get("content-range")
    if got != expect:
        raise RangeNotHonoured(f"Content-Range {got!r} != {expect!r}")
    if len(body) != end - start + 1:
        raise RangeNotHonoured(f"received {len(body)} bytes, expected {end - start + 1}")
    return bytes(body)
