"""Shared SQLite fingerprint store + stat-first reconciliation (M4A-1).

Answers "what changed since the last verified copy" as a persisted, restart-safe, cheap-to-answer fact. Domain-neutral: no application imports, no Dropbox or source-selection
policy, no copying. Each application owns its own database file (``app`` namespace is checked on open) and one *edge* per copy relationship (e.g. ``live->archive``).

Record: ``edge, path, size, mtime_ns, content_hash, hash_algo, state, verified_at, last_seen_at``. States: CLEAN, DIRTY, NEW, MISSING, CONFLICT, UNKNOWN.
``UNKNOWN`` is never clean (it is dirty for every consumer); uncertainty (unreadable directory, unreadable file, imported evidence) never becomes CLEAN or MISSING.

Dirty rule (``reconcile``): stat ``(size, mtime_ns)`` first. No record -> NEW (not hashed). Unchanged stat -> nothing is re-hashed. Changed stat -> hash before deciding;
equal to the stored verified hash -> CLEAN again (touched but identical, stat refreshed, nothing to copy); different -> DIRTY. File gone -> MISSING. Folder state is derived
from the file rows (a folder is CLEAN only if it has rows and every row is CLEAN); directory mtime is never used.

``reconcile`` copies nothing. ``verify`` is read-only. ``full_hash`` is the deliberate safety net (hashes every file, catching same-size/same-mtime edits).
The database is a rebuildable evidence cache: a corrupt or unreadable database raises :class:`FingerprintStoreCorrupt` and is NEVER silently replaced by an empty one; the
caller decides to :meth:`FingerprintStore.quarantine_corrupt` and rebuild (an empty store reports every file NEW, i.e. dirty).
"""

from __future__ import annotations

import enum
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator, Mapping, Optional, Sequence

from shane_common.runtime.transfer.hashing import sha256_file

SCHEMA_VERSION = 1


class FingerprintState(str, enum.Enum):
    CLEAN = "CLEAN"
    DIRTY = "DIRTY"
    NEW = "NEW"
    MISSING = "MISSING"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


CLEAN, DIRTY, NEW, MISSING, CONFLICT, UNKNOWN = (FingerprintState.CLEAN, FingerprintState.DIRTY, FingerprintState.NEW, FingerprintState.MISSING,
                                                  FingerprintState.CONFLICT, FingerprintState.UNKNOWN)


class FingerprintStoreError(Exception):
    pass


class FingerprintStoreCorrupt(FingerprintStoreError):
    """The database is unreadable / fails its integrity check. Fail closed: never treated as clean, never auto-recreated."""


class FingerprintSchemaTooNew(FingerprintStoreError):
    pass


class ReconcileError(FingerprintStoreError):
    """The scan could not be trusted (e.g. root missing/unreadable). Nothing was marked MISSING or CLEAN."""


@dataclass(frozen=True)
class FingerprintRecord:
    edge: str
    path: str
    size: Optional[int]
    mtime_ns: Optional[int]
    content_hash: Optional[str]
    hash_algo: Optional[str]
    state: FingerprintState
    verified_at: Optional[float]
    last_seen_at: Optional[float]


@dataclass
class ReconcileReport:
    edge: str
    root: str
    scanned: int = 0
    unchanged: int = 0
    new: int = 0
    dirty: int = 0
    touched_identical: int = 0
    missing: int = 0
    unknown: int = 0
    hashed: int = 0
    skipped_links: int = 0
    same_stat_mismatch: list = field(default_factory=list)      # full_hash only: content differs although (size, mtime_ns) did not
    errors: list = field(default_factory=list)
    complete: bool = False
    seconds: float = 0.0


@dataclass(frozen=True)
class FolderSummary:
    path: str
    state: FingerprintState          # CLEAN only when there are rows and all are CLEAN; UNKNOWN when there are no rows
    counts: Mapping[str, int]


@dataclass
class VerifyReport:
    edge: str
    new: list = field(default_factory=list)
    stat_changed: list = field(default_factory=list)            # stat differs from the fingerprint: dirty-suspect, NOT hashed here
    missing: list = field(default_factory=list)
    not_clean: dict = field(default_factory=dict)               # stored state -> paths (DIRTY/CONFLICT/UNKNOWN/MISSING/NEW rows)
    folders: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    status: Mapping[str, object] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not (self.new or self.stat_changed or self.missing or self.not_clean or self.errors) and bool(self.status.get("complete"))


_SCHEMA_V1 = """
CREATE TABLE fingerprints (
  edge TEXT NOT NULL, path TEXT NOT NULL, size INTEGER, mtime_ns INTEGER, content_hash TEXT, hash_algo TEXT,
  state TEXT NOT NULL, verified_at REAL, last_seen_at REAL, seen_run INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (edge, path)) WITHOUT ROWID;
CREATE INDEX ix_fingerprints_state ON fingerprints(edge, state);
CREATE TABLE edge_meta (edge TEXT NOT NULL, k TEXT NOT NULL, v TEXT NOT NULL, PRIMARY KEY (edge, k)) WITHOUT ROWID;
"""
_MIGRATIONS: dict = {1: _SCHEMA_V1}
_COLS = "edge, path, size, mtime_ns, content_hash, hash_algo, state, verified_at, last_seen_at"
_SYNC = ("NORMAL", "FULL", "EXTRA")


def _rec(row: Sequence) -> FingerprintRecord:
    return FingerprintRecord(row[0], row[1], row[2], row[3], row[4], row[5], FingerprintState(row[6]), row[7], row[8])


class FingerprintStore:
    """One SQLite file for one application (``app`` namespace). Narrow interface: lookup, record_verified(_many), mark_dirty, list_by_state, reconcile, verify, full_hash."""

    def __init__(self, db_path: Path | str, *, app: str, synchronous: str = "FULL", integrity: str = "full", clock: Callable[[], float] = time.time) -> None:
        if synchronous.upper() not in _SYNC:
            raise ValueError(f"synchronous must be one of {_SYNC}")
        if integrity not in ("full", "quick"):
            raise ValueError("integrity must be 'full' or 'quick'")
        self._path = Path(db_path)
        self._app = app
        self._clock = clock
        self._lock = threading.RLock()
        existed = self._path.exists()
        if existed and self._path.stat().st_size == 0:
            raise FingerprintStoreCorrupt(f"{self._path}: existing zero-length database (truncated?); refusing to treat as a fresh store")
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self._path), isolation_level=None, check_same_thread=False, timeout=5.0)
        try:
            self._open(existed, synchronous.upper(), integrity)
        except BaseException:
            self._db.close()
            raise

    # -- open / schema -------------------------------------------------------------------------------------------------------------
    def _open(self, existed: bool, synchronous: str, integrity: str) -> None:
        db = self._db
        try:
            check = db.execute(f"PRAGMA {'integrity_check' if integrity == 'full' else 'quick_check'}").fetchall()
            if [r[0] for r in check] != ["ok"]:
                raise FingerprintStoreCorrupt(f"{self._path}: integrity check failed: {[r[0] for r in check][:3]}")
            mode = db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if str(mode).lower() != "wal":
                raise FingerprintStoreError(f"{self._path}: WAL journal mode unavailable (got {mode!r})")
            db.execute(f"PRAGMA synchronous={synchronous}")
            db.execute("PRAGMA busy_timeout=5000")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            has_tables = db.execute("SELECT count(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            if version == 0 and has_tables and existed:
                raise FingerprintStoreCorrupt(f"{self._path}: tables present without a schema version")
            if version > SCHEMA_VERSION:
                raise FingerprintSchemaTooNew(f"{self._path}: schema {version} is newer than supported {SCHEMA_VERSION}")
            for v in range(version + 1, SCHEMA_VERSION + 1):
                db.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in _MIGRATIONS[v].split(";"):
                        if stmt.strip():
                            db.execute(stmt)
                    if v == 1:
                        db.execute("CREATE TABLE store_meta (k TEXT PRIMARY KEY, v TEXT NOT NULL) WITHOUT ROWID")
                        db.execute("INSERT INTO store_meta VALUES ('app', ?)", (self._app,))
                        db.execute("INSERT INTO store_meta VALUES ('created_at', ?)", (repr(self._clock()),))
                    db.execute(f"PRAGMA user_version={v}")
                    db.execute("COMMIT")
                except BaseException:
                    db.execute("ROLLBACK")
                    raise
            row = db.execute("SELECT v FROM store_meta WHERE k='app'").fetchone()
        except sqlite3.DatabaseError as e:
            raise FingerprintStoreCorrupt(f"{self._path}: unreadable database: {e}") from e
        if row is None or row[0] != self._app:
            raise FingerprintStoreError(f"{self._path}: belongs to app {row and row[0]!r}, not {self._app!r}")

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def __enter__(self) -> "FingerprintStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @staticmethod
    def quarantine_corrupt(db_path: Path | str) -> Path:
        """Explicit, caller-invoked recovery step: move a corrupt database (and WAL sidecars) aside so the next open creates an EMPTY store (every file then reports NEW = dirty)."""
        p = Path(db_path)
        dest = p.with_name(f"{p.name}.corrupt-{int(time.time())}")
        i = 0
        while dest.exists():
            i += 1
            dest = p.with_name(f"{p.name}.corrupt-{int(time.time())}-{i}")
        os.replace(p, dest)
        for suffix in ("-wal", "-shm"):
            side = Path(str(p) + suffix)
            if side.exists():
                os.replace(side, Path(str(dest) + suffix))
        return dest

    # -- transactions --------------------------------------------------------------------------------------------------------------
    def _tx(self, statements: Iterable[tuple]) -> None:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                for sql, args in statements:
                    self._db.execute(sql, args)
                self._db.execute("COMMIT")
            except BaseException:
                self._db.execute("ROLLBACK")
                raise

    # -- narrow interface ----------------------------------------------------------------------------------------------------------
    def lookup(self, edge: str, path: str) -> Optional[FingerprintRecord]:
        with self._lock:
            row = self._db.execute(f"SELECT {_COLS} FROM fingerprints WHERE edge=? AND path=?", (edge, path)).fetchone()
        return _rec(row) if row else None

    def state_of(self, edge: str, path: str) -> FingerprintState:
        r = self.lookup(edge, path)
        return r.state if r else NEW

    def record_verified(self, edge: str, path: str, *, size: int, mtime_ns: int, content_hash: str, hash_algo: str = "sha256") -> None:
        self.record_verified_many(edge, [(path, size, mtime_ns, content_hash, hash_algo)])

    def record_verified_many(self, edge: str, items: Iterable[tuple]) -> None:
        """One transaction for many ``(path, size, mtime_ns, content_hash, hash_algo)``. Only a verified copy / verified hash match may call this: it is what makes a row CLEAN."""
        now = self._clock()
        self._tx(("INSERT INTO fingerprints (edge, path, size, mtime_ns, content_hash, hash_algo, state, verified_at, last_seen_at, seen_run) VALUES (?,?,?,?,?,?,'CLEAN',?,?,0) "
                  "ON CONFLICT(edge, path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, content_hash=excluded.content_hash, hash_algo=excluded.hash_algo, "
                  "state='CLEAN', verified_at=excluded.verified_at, last_seen_at=excluded.last_seen_at", (edge, p, int(s), int(m), h, a, now, now)) for p, s, m, h, a in items)

    def mark_dirty(self, edge: str, path: str, state: FingerprintState = DIRTY) -> None:
        """Consumer-side downgrade (DIRTY, CONFLICT or UNKNOWN). Creates a row without stat when the path has no record. CLEAN/NEW/MISSING cannot be set here."""
        state = FingerprintState(state)
        if state not in (DIRTY, CONFLICT, UNKNOWN):
            raise ValueError("mark_dirty accepts DIRTY, CONFLICT or UNKNOWN only")
        self._tx([("INSERT INTO fingerprints (edge, path, state, last_seen_at, seen_run) VALUES (?,?,?,?,0) ON CONFLICT(edge, path) DO UPDATE SET state=excluded.state",
                   (edge, path, state.value, self._clock()))])

    def import_unknown(self, edge: str, items: Iterable[tuple]) -> int:
        """Import prior evidence ``(path, content_hash, hash_algo)`` (no mtime): rows start UNKNOWN and are stat+hashed once by the next reconcile, never assumed clean.
        Existing rows are left untouched."""
        n = 0
        stmts = []
        for p, h, a in items:
            stmts.append(("INSERT OR IGNORE INTO fingerprints (edge, path, content_hash, hash_algo, state, last_seen_at, seen_run) VALUES (?,?,?,?,'UNKNOWN',?,0)", (edge, p, h, a, self._clock())))
            n += 1
        self._tx(stmts)
        return n

    def list_by_state(self, edge: str, state: FingerprintState) -> list:
        with self._lock:
            rows = self._db.execute(f"SELECT {_COLS} FROM fingerprints WHERE edge=? AND state=? ORDER BY path", (edge, FingerprintState(state).value)).fetchall()
        return [_rec(r) for r in rows]

    def dirty_set(self, edge: str) -> list:
        """Everything a backup must (re)consider: every state except CLEAN (UNKNOWN counts as dirty). MISSING rows are listed so deletions are visible."""
        with self._lock:
            rows = self._db.execute(f"SELECT {_COLS} FROM fingerprints WHERE edge=? AND state!='CLEAN' ORDER BY path", (edge,)).fetchall()
        return [_rec(r) for r in rows]

    def counts(self, edge: str) -> dict:
        with self._lock:
            return {s: n for s, n in self._db.execute("SELECT state, count(*) FROM fingerprints WHERE edge=? GROUP BY state", (edge,))}

    # -- edge status (readable without any host running) ------------------------------------------------------------------------
    def edge_status(self, edge: str) -> dict:
        with self._lock:
            meta = dict(self._db.execute("SELECT k, v FROM edge_meta WHERE edge=?", (edge,)).fetchall())
        done = float(meta["last_completed_at"]) if "last_completed_at" in meta else None
        return {"edge": edge, "last_completed_at": done, "age_seconds": (self._clock() - done) if done is not None else None, "never_reconciled": done is None,
                "interrupted": meta.get("run_state") == "RUNNING", "complete": meta.get("run_state") == "COMPLETE" and done is not None, "counts": self.counts(edge)}

    def _set_meta(self, edge: str, **kv: str) -> None:
        self._tx([("INSERT INTO edge_meta VALUES (?,?,?) ON CONFLICT(edge, k) DO UPDATE SET v=excluded.v", (edge, k, v)) for k, v in kv.items()])

    # -- folder-level state --------------------------------------------------------------------------------------------------------
    def folder_state(self, edge: str, folder: str = "") -> FolderSummary:
        """Derived from the file rows under ``folder`` (children added = NEW rows, removed = MISSING rows); never from directory mtime."""
        folder = folder.strip("/")
        with self._lock:
            if folder:
                rows = self._db.execute("SELECT state, count(*) FROM fingerprints WHERE edge=? AND path>=? AND path<? GROUP BY state", (edge, folder + "/", folder + "0")).fetchall()
            else:
                rows = self._db.execute("SELECT state, count(*) FROM fingerprints WHERE edge=? GROUP BY state", (edge,)).fetchall()
        counts = dict(rows)
        return FolderSummary(folder, _fold(counts), counts)

    def folder_states(self, edge: str, depth: int = 1) -> list:
        """Per-folder summary at ``depth`` path segments, derived from the file rows (never from directory mtime)."""
        agg: dict = {}
        with self._lock:
            rows = self._db.execute("SELECT path, state FROM fingerprints WHERE edge=?", (edge,)).fetchall()
        for p, s in rows:
            parts = p.split("/")
            key = "/".join(parts[:depth]) if len(parts) > depth else ""
            agg.setdefault(key, {}).setdefault(s, 0)
            agg[key][s] += 1
        return [FolderSummary(k, _fold(c), c) for k, c in sorted(agg.items())]

    # -- scan machinery --------------------------------------------------------------------------------------------------------------
    def reconcile(self, edge: str, root: Path | str, *, hash_fn: Callable[[Path], str] = sha256_file, hash_algo: str = "sha256",
                  skip: Optional[Callable[[str, bool], bool]] = None, batch_size: int = 500, _force_hash: bool = False) -> ReconcileReport:
        """Cheap stat walk. Updates the dirty set; copies nothing; hashes only files whose (size, mtime_ns) changed (or every file under ``full_hash``).

        Fails closed: a root that is missing or not a directory raises :class:`ReconcileError` before any change. Unreadable directories or files make the affected rows
        UNKNOWN (never CLEAN, never MISSING) and the report ``complete=False``. Progress is committed per batch; killing the run at any point is safe (the edge then reports
        ``interrupted`` until a run completes) and a rerun simply continues."""
        root_s = os.fspath(root)
        if not os.path.isdir(root_s):
            raise ReconcileError(f"scan root is missing or not a directory: {root_s}")
        t0 = time.perf_counter()
        rep = ReconcileReport(edge, root_s)
        with self._lock:
            run_id = int(dict(self._db.execute("SELECT k, v FROM edge_meta WHERE edge=?", (edge,)).fetchall()).get("run_counter", "0")) + 1
        self._set_meta(edge, run_counter=str(run_id), run_state="RUNNING")
        bad_dirs: list = []
        pending: list = []

        def flush() -> None:
            if pending:
                self._tx(list(pending))
                pending.clear()

        for rel, st in self._walk(root_s, skip, rep, bad_dirs):
            rep.scanned += 1
            cur = self.lookup(edge, rel)
            stmt = self._decide(edge, rel, root_s, st, cur, run_id, hash_fn, hash_algo, rep, _force_hash)
            pending.append(stmt)
            if len(pending) >= batch_size:
                flush()
        flush()
        # MISSING: only rows not seen this run and not under a directory we could not list; uncertainty -> UNKNOWN
        now = self._clock()
        under_bad = lambda p: any(p == d or p.startswith(d + "/") for d in bad_dirs)        # noqa: E731
        with self._lock:
            stale = self._db.execute("SELECT path, state FROM fingerprints WHERE edge=? AND seen_run!=?", (edge, run_id)).fetchall()
        stmts = []
        for p, s in stale:
            if under_bad(p):
                if s == CLEAN.value:
                    stmts.append(("UPDATE fingerprints SET state='UNKNOWN', seen_run=? WHERE edge=? AND path=?", (run_id, edge, p)))
                    rep.unknown += 1
            elif skip is not None and skip(p, False):
                stmts.append(("DELETE FROM fingerprints WHERE edge=? AND path=?", (edge, p)))               # now excluded by source policy: no longer part of the protected set
            elif s != MISSING.value:
                stmts.append(("UPDATE fingerprints SET state='MISSING', seen_run=?, last_seen_at=? WHERE edge=? AND path=?", (run_id, now, edge, p)))
                rep.missing += 1
            else:
                stmts.append(("UPDATE fingerprints SET seen_run=? WHERE edge=? AND path=?", (run_id, edge, p)))
        for i in range(0, len(stmts), batch_size):
            self._tx(stmts[i:i + batch_size])
        rep.complete = not rep.errors
        rep.seconds = time.perf_counter() - t0
        self._set_meta(edge, run_state="COMPLETE" if rep.complete else "INCOMPLETE", **({"last_completed_at": repr(self._clock())} if rep.complete else {}))
        return rep

    def full_hash(self, edge: str, root: Path | str, **kw) -> ReconcileReport:
        """Safety net: hash every file regardless of stat. A CLEAN row whose content changed under an unchanged (size, mtime_ns) becomes DIRTY and is listed in
        ``same_stat_mismatch`` (silent corruption / same-stat edit)."""
        return self.reconcile(edge, root, _force_hash=True, **kw)

    def _walk(self, root: str, skip, rep: ReconcileReport, bad_dirs: list) -> Iterator[tuple]:
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            abs_dir = os.path.join(root, rel_dir) if rel_dir else root
            try:
                with os.scandir(abs_dir) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError as e:
                rep.errors.append((rel_dir or ".", f"scandir: {e!r}"))
                bad_dirs.append(rel_dir)
                continue
            for e in entries:
                rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
                try:
                    if e.is_symlink() or _is_reparse(e):
                        rep.skipped_links += 1                      # never follow symlinks / junctions / mount points out of the root
                        continue
                    is_dir = e.is_dir(follow_symlinks=False)
                    if skip is not None and skip(rel, is_dir):
                        continue
                    if is_dir:
                        stack.append(rel)
                    elif e.is_file(follow_symlinks=False):
                        yield rel, e.stat(follow_symlinks=False)
                except OSError as ex:
                    rep.errors.append((rel, f"stat: {ex!r}"))
                    bad_dirs.append(rel)                            # treat the entry itself as an unlistable prefix: its row (if any) becomes UNKNOWN, not MISSING

    def _decide(self, edge, rel, root, st, cur, run_id, hash_fn, algo, rep, force) -> tuple:
        now = self._clock()
        size, mtime = st.st_size, st.st_mtime_ns
        upsert = ("INSERT INTO fingerprints (edge, path, size, mtime_ns, content_hash, hash_algo, state, verified_at, last_seen_at, seen_run) VALUES (?,?,?,?,?,?,?,?,?,?) "
                  "ON CONFLICT(edge, path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, content_hash=excluded.content_hash, hash_algo=excluded.hash_algo, "
                  "state=excluded.state, verified_at=excluded.verified_at, last_seen_at=excluded.last_seen_at, seen_run=excluded.seen_run")

        def row(state, h, a, verified):
            return (upsert, (edge, rel, size, mtime, h, a, state.value, verified, now, run_id))
        if cur is None:
            rep.new += 1
            return row(NEW, None, None, None)
        same_stat = cur.size == size and cur.mtime_ns == mtime
        if cur.state == CONFLICT:                                   # sticky until record_verified resolves it
            rep.unchanged += same_stat
            return row(CONFLICT, cur.content_hash, cur.hash_algo, cur.verified_at)
        if cur.state == NEW and cur.mtime_ns is not None and not force:
            rep.unchanged += same_stat
            return row(NEW, None, None, None)
        needs_hash = force or not same_stat or cur.state in (UNKNOWN, MISSING) or cur.mtime_ns is None
        if not needs_hash:
            rep.unchanged += 1
            return row(cur.state, cur.content_hash, cur.hash_algo, cur.verified_at)
        try:
            h = hash_fn(Path(root, *rel.split("/")))
            rep.hashed += 1
        except OSError as e:
            rep.errors.append((rel, f"hash: {e!r}"))
            rep.unknown += 1
            return row(UNKNOWN, cur.content_hash, cur.hash_algo, cur.verified_at)
        baseline_ok = cur.content_hash is not None and cur.hash_algo == algo
        if baseline_ok and h == cur.content_hash:
            if same_stat and cur.state == CLEAN:
                rep.unchanged += 1
            else:
                rep.touched_identical += 1
            return row(CLEAN, h, algo, cur.verified_at if cur.state == CLEAN else self._clock())
        if cur.state == NEW and not baseline_ok:
            return row(NEW, None, None, None)
        if force and same_stat and cur.state == CLEAN and baseline_ok:
            rep.same_stat_mismatch.append(rel)
        rep.dirty += 1
        return row(DIRTY, cur.content_hash if baseline_ok else None, cur.hash_algo if baseline_ok else None, cur.verified_at)

    # -- read-only verify ------------------------------------------------------------------------------------------------------------
    def verify(self, edge: str, root: Path | str, *, folder_depth: int = 1, skip: Optional[Callable[[str, bool], bool]] = None) -> VerifyReport:
        """Read-only drift report: stat walk compared with the stored fingerprints. Writes nothing, hashes nothing, copies nothing. A file whose stat changed is reported
        ``stat_changed`` (dirty-suspect); only ``reconcile`` decides by hash."""
        root_s = os.fspath(root)
        if not os.path.isdir(root_s):
            raise ReconcileError(f"scan root is missing or not a directory: {root_s}")
        rep = VerifyReport(edge)
        scratch = ReconcileReport(edge, root_s)
        bad: list = []
        seen = set()
        with self._lock:
            stored = {r[1]: r for r in self._db.execute(f"SELECT {_COLS} FROM fingerprints WHERE edge=?", (edge,)).fetchall()}
        for rel, st in self._walk(root_s, skip, scratch, bad):
            seen.add(rel)
            row = stored.get(rel)
            if row is None:
                rep.new.append(rel)
            elif row[2] != st.st_size or row[3] != st.st_mtime_ns:
                rep.stat_changed.append(rel)
        for p, row in stored.items():
            if p not in seen and not any(p == d or p.startswith(d + "/") for d in bad) and row[6] != MISSING.value:
                rep.missing.append(p)
            if row[6] != CLEAN.value:
                rep.not_clean.setdefault(row[6], []).append(p)
        rep.errors = list(scratch.errors)
        rep.folders = self.folder_states(edge, folder_depth)
        rep.status = self.edge_status(edge)
        return rep


def _is_reparse(entry: "os.DirEntry") -> bool:
    attrs = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
    return bool(attrs & 0x400)                                      # FILE_ATTRIBUTE_REPARSE_POINT (junctions are not reported by is_symlink on 3.11)


def _fold(counts: Mapping[str, int]) -> FingerprintState:
    if not counts:
        return UNKNOWN
    return CLEAN if set(counts) == {CLEAN.value} else DIRTY
