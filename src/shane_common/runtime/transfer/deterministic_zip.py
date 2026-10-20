"""Deterministic ZIP construction (application-neutral).

Same ordered member list + same options => identical archive bytes (for one zlib build): sorted names, fixed timestamp/attributes, no extra fields, no comment, no data
descriptors beyond what zipfile emits deterministically. The ZIP carries NO authoritative metadata: names, sizes and SHA-256 hashes live in the caller's manifest, which is
built from the `entries` returned here (hash of the exact bytes archived, taken in the same pass).

Sources are opened read-only. A member whose size/mtime changed across the read (or that shrank) is reported ``stable=False``; the caller decides whether to drop it.
"""

from __future__ import annotations

import hashlib
import os
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .dropbox_destination import hash_file_both

MIB = 1024 * 1024
COPY_BUF = MIB
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_REGULAR_FILE_ATTR = 0o100644 << 16


class StagingBudgetExceeded(RuntimeError):
    pass


class StagingBudget:
    """Hard bound on temporary artifacts under ``root`` (the owner approved 300 MiB for M5-5)."""

    def __init__(self, root: str | os.PathLike, limit: int = 300 * MIB) -> None:
        self.root, self.limit, self.peak = Path(root), int(limit), 0
        self.root.mkdir(parents=True, exist_ok=True)

    def used(self) -> int:
        return sum(p.stat().st_size for p in self.root.rglob("*") if p.is_file())

    def check(self, extra: int = 0) -> int:
        u = self.used()
        self.peak = max(self.peak, u + extra)
        if u + extra > self.limit:
            raise StagingBudgetExceeded(f"staging budget exceeded: {u + extra} > {self.limit}")
        return u


@dataclass(frozen=True)
class ZipSource:
    arcname: str                  # name inside the archive (caller has validated it)
    path: str                     # local file to read
    deflate: bool = True


@dataclass
class ZipBuildResult:
    entries: list = field(default_factory=list)   # {path,size,sha256,crc32,stored_size,mtime_ns,stable,compression}
    size: int = 0
    sha256: str = ""
    dropbox_hash: str = ""
    build_seconds: float = 0.0
    hash_seconds: float = 0.0
    unstable: list = field(default_factory=list)  # arcnames
    out_path: str = ""


class _Reader:
    """Reads EXACTLY ``size`` bytes, hashing what is archived; a short read raises (source shrank)."""

    def __init__(self, fh: Any, size: int) -> None:
        self._fh, self.remaining, self.sha, self.crc = fh, size, hashlib.sha256(), 0

    def read(self, n: int = -1) -> bytes:
        if self.remaining <= 0:
            return b""
        n = self.remaining if n is None or n < 0 else min(n, self.remaining)
        data = self._fh.read(n)
        if not data:
            raise IOError("source shrank while it was being archived")
        self.remaining -= len(data)
        self.sha.update(data)
        self.crc = zlib.crc32(data, self.crc)
        return data


def write_deterministic_zip(sources: Iterable[ZipSource], out_path: str | os.PathLike, *, compresslevel: int = 6, budget: Optional[StagingBudget] = None,
                            estimate_bytes: Optional[int] = None) -> ZipBuildResult:
    """Write ``sources`` (sorted here by arcname, then path) to ``out_path`` via a ``.partial`` file renamed on success. Duplicate arcnames are refused."""
    out_path = Path(out_path)
    items = sorted(sources, key=lambda s: (s.arcname.encode("utf-8"), s.path))
    names = [s.arcname for s in items]
    if len(set(names)) != len(names):
        raise ValueError("duplicate archive member names")
    if not 1 <= compresslevel <= 9:
        raise ValueError("compresslevel must be 1..9")
    if budget is not None:
        total = estimate_bytes if estimate_bytes is not None else sum(os.stat(s.path).st_size for s in items)
        budget.check(int(total) + 4 * MIB)
    tmp = out_path.with_name(out_path.name + ".partial")
    res = ZipBuildResult(out_path=str(out_path))
    t0 = time.perf_counter()
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=compresslevel, allowZip64=True) as zf:
            for s in items:
                st1 = os.stat(s.path)
                zi = zipfile.ZipInfo(s.arcname, date_time=FIXED_DATE_TIME)
                zi.compress_type = zipfile.ZIP_DEFLATED if s.deflate else zipfile.ZIP_STORED
                zi._compresslevel = compresslevel if s.deflate else None      # zipfile's own slot; the public API only exposes it per ZipFile/writestr
                zi.create_system, zi.external_attr = 3, _REGULAR_FILE_ATTR
                with open(s.path, "rb") as fh:
                    rd = _Reader(fh, st1.st_size)
                    with zf.open(zi, "w", force_zip64=st1.st_size >= 0x7FFFFFFF) as w:
                        while True:
                            b = rd.read(COPY_BUF)
                            if not b:
                                break
                            w.write(b)
                    extra = fh.read(1)                                    # a file that grew during the read is unstable
                st2 = os.stat(s.path)
                stable = (st1.st_size, st1.st_mtime_ns) == (st2.st_size, st2.st_mtime_ns) and not extra
                if not stable:
                    res.unstable.append(s.arcname)
                res.entries.append({"path": s.arcname, "size": st1.st_size, "sha256": rd.sha.hexdigest(), "crc32": f"{rd.crc & 0xFFFFFFFF:08x}",
                                    "stored_size": zi.compress_size, "mtime_ns": st1.st_mtime_ns, "stable": stable,
                                    "compression": "deflate" if s.deflate else "stored"})
        os.replace(tmp, out_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    res.build_seconds = time.perf_counter() - t0
    t1 = time.perf_counter()
    res.sha256, res.dropbox_hash, res.size = hash_file_both(out_path)
    res.hash_seconds = time.perf_counter() - t1
    return res
