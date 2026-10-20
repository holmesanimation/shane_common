"""Hardened ZIP extraction for restores (application-neutral).

Every rule is enforced HERE, not by callers: Windows-safe names, case-insensitive collisions (also across packages through a shared ``NameRegistry``), links/special/encrypted
entries, members not declared in the manifest, declared-vs-actual size, expansion ratio, member/byte/depth/path-length limits, archive SHA-256 before opening, per-member SHA-256
after extraction, free-space preflight, new-or-empty target, and forbidden roots (e.g. the canonical MarketData tree). A file reaches its final name only after its hash matched,
and a package is extracted all-or-nothing: members are first written and verified under ``<target>/.restore_tmp`` and only then moved into place.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

MIB = 1024 * 1024
COPY_BUF = MIB
TMP_DIR = ".restore_tmp"
HARD_MAX_MEMBERS = 1_000_000                       # ceilings a caller cannot raise through ExtractLimits
HARD_MAX_MEMBER_BYTES = 64 * 1024 * MIB
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$", *(f"COM{i}" for i in [*range(1, 10), "¹", "²", "³"]), *(f"LPT{i}" for i in [*range(1, 10), "¹", "²", "³"])})
_BAD_CHARS = frozenset('<>:"|?*\\')


class UnsafeArchive(Exception):
    """Archive, name, target or limit violation. Nothing partially verified is left under a final name."""


class ArchiveHashMismatch(UnsafeArchive):
    pass


@dataclass(frozen=True)
class ExtractLimits:
    max_members: int = 200_000
    max_total_bytes: int = 8 * 1024 * MIB
    max_member_bytes: int = 4 * 1024 * MIB
    max_ratio: float = 200.0
    max_rel_len: int = 240
    max_abs_len: int = 259                          # classic MAX_PATH; raise explicitly (with long paths enabled) for a restore run
    max_depth: int = 32
    min_free_bytes: int = 64 * MIB

    def clamped(self) -> "ExtractLimits":
        return ExtractLimits(min(self.max_members, HARD_MAX_MEMBERS), self.max_total_bytes, min(self.max_member_bytes, HARD_MAX_MEMBER_BYTES), self.max_ratio,
                             self.max_rel_len, self.max_abs_len, self.max_depth, self.min_free_bytes)


def validate_rel(path: str, limits: ExtractLimits = ExtractLimits()) -> None:
    if not isinstance(path, str) or not path or len(path) > limits.max_rel_len:
        raise UnsafeArchive(f"bad name length: {str(path)[:80]!r}")
    if unicodedata.normalize("NFC", path) != path:
        raise UnsafeArchive(f"name is not NFC-normalized: {path[:80]!r}")
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise UnsafeArchive(f"control character in name: {path[:80]!r}")
    if path.startswith("/") or "\\" in path:
        raise UnsafeArchive(f"absolute or backslash name: {path[:80]!r}")
    if ":" in path:
        raise UnsafeArchive(f"drive letter or alternate data stream: {path[:80]!r}")
    segs = path.split("/")
    if len(segs) > limits.max_depth:
        raise UnsafeArchive(f"path too deep: {path[:80]!r}")
    for seg in segs:
        if seg in ("", ".", "..") or seg.endswith((".", " ")) or any(c in _BAD_CHARS for c in seg) or seg.split(".")[0].upper() in _RESERVED:
            raise UnsafeArchive(f"unsafe component {seg!r} in {path[:80]!r}")


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


class NameRegistry:
    """Detects exact duplicates, case-insensitive collisions and file/directory conflicts over a WHOLE restore set (share one instance across packages)."""

    def __init__(self) -> None:
        self._files: dict = {}
        self._dirs: set = set()

    def add(self, name: str) -> None:
        key = _fold(name)
        if key in self._files:
            raise UnsafeArchive(f"duplicate or case-insensitive collision: {name!r} vs {self._files[key]!r}")
        if key in self._dirs:
            raise UnsafeArchive(f"file/directory conflict at {name!r}")
        parts = key.split("/")
        for i in range(1, len(parts)):
            parent = "/".join(parts[:i])
            if parent in self._files:
                raise UnsafeArchive(f"file/directory conflict: {name!r} below file {self._files[parent]!r}")
        self._files[key] = name
        for i in range(1, len(parts)):
            self._dirs.add("/".join(parts[:i]))

    def check_all(self, names) -> None:
        for n in sorted(names):
            self.add(n)


def prepare_target(target: str | os.PathLike, forbidden_roots: tuple = ()) -> Path:
    """The restore target must be new or empty and must not equal, contain or sit inside any forbidden root (e.g. the canonical MarketData tree)."""
    t = Path(target)
    tr = os.path.normcase(os.path.abspath(t))
    for fr in forbidden_roots:
        f = os.path.normcase(os.path.abspath(fr))
        if tr == f or tr.startswith(f + os.sep) or f.startswith(tr + os.sep):
            raise UnsafeArchive(f"restore target {str(t)!r} overlaps a protected root {str(fr)!r}")
    if t.exists():
        if not t.is_dir() or any(t.iterdir()):
            raise UnsafeArchive(f"restore target {str(t)!r} must be a new or empty directory")
    else:
        t.mkdir(parents=True)
    return t


def sha256_file(path: str | os.PathLike) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(COPY_BUF)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def extract_zip(path: str | os.PathLike, manifest_members: list, target: str | os.PathLike, *, expected_sha256: Optional[str] = None,
                wanted: Optional[Callable[[str], bool]] = None, limits: ExtractLimits = ExtractLimits(), registry: Optional[NameRegistry] = None,
                tmp_dir: Optional[str | os.PathLike] = None) -> dict:
    """Validate, then extract the ``wanted`` members (default all declared). ``manifest_members`` = [{path,size,sha256,mtime_ns?}]. Raises UnsafeArchive on any violation.
    ``tmp_dir`` (default ``<target>/.restore_tmp``) must be on the same volume as ``target``; concurrent extractions into one target need distinct ``tmp_dir`` values."""
    limits = limits.clamped()
    target = Path(target)
    declared = {m["path"]: m for m in manifest_members}
    if len(declared) != len(manifest_members):
        raise UnsafeArchive("manifest declares duplicate paths")
    if expected_sha256 is not None and sha256_file(path) != expected_sha256:
        raise ArchiveHashMismatch(f"package SHA-256 does not match the manifest: {Path(path).name}")
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise UnsafeArchive(f"not a valid zip: {e}")
    local = registry if registry is not None else NameRegistry()
    work: list = []
    try:
        with zf:
            seen_in_zip: set = set()
            total = 0
            infos = zf.infolist()
            if len(infos) > limits.max_members:
                raise UnsafeArchive("too many members")
            for zi in infos:
                name = zi.filename
                validate_rel(name, limits)
                mode = zi.external_attr >> 16
                if zi.is_dir() or stat.S_IFMT(mode) not in (0, stat.S_IFREG) or zi.flag_bits & 0x1:
                    raise UnsafeArchive(f"not a plain regular file (dir/link/device/encrypted): {name!r}")
                if zi.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                    raise UnsafeArchive(f"unsupported compression for {name!r}")
                if name in seen_in_zip:
                    raise UnsafeArchive(f"duplicate member {name!r}")
                seen_in_zip.add(name)
                if name not in declared:
                    raise UnsafeArchive(f"member not in manifest: {name!r}")
                size = zi.file_size
                if size != declared[name]["size"] or size > limits.max_member_bytes:
                    raise UnsafeArchive(f"size mismatch or oversize: {name!r}")
                if zi.compress_size and size > MIB and size / zi.compress_size > limits.max_ratio:
                    raise UnsafeArchive(f"suspicious expansion ratio: {name!r}")
                total += size
                if total > limits.max_total_bytes:
                    raise UnsafeArchive("declared total too large")
            missing = [p for p in declared if p not in seen_in_zip]
            if missing:
                raise UnsafeArchive(f"manifest members missing from the archive: {missing[:3]}")
            sel = [zi for zi in infos if wanted is None or wanted(zi.filename)]
            root = os.path.abspath(target)
            for zi in sel:
                if len(os.path.join(root, *zi.filename.split("/"))) > limits.max_abs_len:
                    raise UnsafeArchive(f"restored path exceeds {limits.max_abs_len} characters: {zi.filename[:80]!r}")
            need = sum(zi.file_size for zi in sel)
            target.mkdir(parents=True, exist_ok=True)
            if shutil.disk_usage(target).free < need * 2 + limits.min_free_bytes:
                raise UnsafeArchive("insufficient free space on the restore target")
            for zi in sel:
                local.add(zi.filename)
            tmpdir = Path(tmp_dir) if tmp_dir is not None else target / TMP_DIR
            tmpdir.mkdir(parents=True, exist_ok=True)
            # phase 1: write + verify everything under .restore_tmp
            for i, zi in enumerate(sel):
                decl = declared[zi.filename]
                part = tmpdir / f"{i:08d}.part"
                h, got = hashlib.sha256(), 0
                try:
                    with zf.open(zi) as src, open(part, "wb") as out:
                        while True:
                            b = src.read(COPY_BUF)
                            if not b:
                                break
                            got += len(b)
                            if got > zi.file_size:
                                raise UnsafeArchive(f"member longer than declared: {zi.filename!r}")
                            h.update(b)
                            out.write(b)
                except zipfile.BadZipFile as e:
                    raise UnsafeArchive(f"corrupt member {zi.filename!r}: {e}")
                if got != zi.file_size or h.hexdigest() != decl["sha256"]:
                    raise UnsafeArchive(f"truncated or corrupt member (hash mismatch): {zi.filename!r}")
                work.append((part, zi.filename, decl))
            # phase 2: every member verified; move into place (never overwrite)
            ext_bytes = 0
            for part, name, decl in work:
                dest = target.joinpath(*name.split("/"))
                if os.path.commonpath([root, os.path.abspath(dest)]) != root:
                    raise UnsafeArchive(f"escapes the restore root: {name!r}")
                if dest.exists():
                    raise UnsafeArchive(f"refusing to overwrite {name!r}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                os.replace(part, dest)
                if decl.get("mtime_ns"):
                    os.utime(dest, ns=(decl["mtime_ns"], decl["mtime_ns"]))
                ext_bytes += decl["size"]
    finally:
        tmpdir = Path(tmp_dir) if tmp_dir is not None else target / TMP_DIR
        if tmpdir.exists():
            shutil.rmtree(tmpdir, ignore_errors=True)
    return {"extracted": len(work), "bytes": ext_bytes, "members_in_archive": len(declared)}
