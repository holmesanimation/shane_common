"""M4A-1: shared SQLite fingerprint store + reconciliation core. tmp_path fixtures only; no application imports; no drive other than the pytest temp volume is named or probed."""

from __future__ import annotations

import ast
import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from shane_common.runtime.transfer import fingerprint_store as fs
from shane_common.runtime.transfer.fingerprint_store import (
    CLEAN, CONFLICT, DIRTY, MISSING, NEW, UNKNOWN, FingerprintSchemaTooNew, FingerprintStore, FingerprintStoreCorrupt, FingerprintStoreError, ReconcileError,
)

EDGE = "live->archive"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class Counter:
    """hash_fn that counts calls."""

    def __init__(self):
        self.paths: list = []

    def __call__(self, p: Path) -> str:
        self.paths.append(p.name)
        return sha(Path(p).read_bytes())


@pytest.fixture()
def tree(tmp_path):
    root = tmp_path / "src"
    (root / "a").mkdir(parents=True)
    (root / "b").mkdir()
    (root / "a" / "one.txt").write_bytes(b"one")
    (root / "a" / "two.txt").write_bytes(b"two!")
    (root / "b" / "three.txt").write_bytes(b"three")
    return root


@pytest.fixture()
def store(tmp_path):
    s = FingerprintStore(tmp_path / "state" / "fp.sqlite", app="trading")
    yield s
    s.close()


def verify_all(store: FingerprintStore, root: Path) -> None:
    """What a backup consumer does after a verified copy: record every file verified with its current stat and hash."""
    items = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            st = p.stat()
            items.append((p.relative_to(root).as_posix(), st.st_size, st.st_mtime_ns, sha(p.read_bytes()), "sha256"))
    store.record_verified_many(EDGE, items)


def settle(store, root, **kw):
    """first reconcile (NEW) -> verified copy -> clean baseline."""
    store.reconcile(EDGE, root, **kw)
    verify_all(store, root)


# ---------------------------------------------------------------------------------------- schema / durability / fail-closed open
def test_schema_wal_synchronous_and_version(tmp_path):
    db = tmp_path / "fp.sqlite"
    with FingerprintStore(db, app="trading", synchronous="FULL") as s:
        raw = s._db
        assert raw.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert raw.execute("PRAGMA synchronous").fetchone()[0] == 2                      # FULL
        assert raw.execute("PRAGMA user_version").fetchone()[0] == fs.SCHEMA_VERSION
        cols = [r[1] for r in raw.execute("PRAGMA table_info(fingerprints)")]
        assert cols[:9] == ["edge", "path", "size", "mtime_ns", "content_hash", "hash_algo", "state", "verified_at", "last_seen_at"]
    with pytest.raises(ValueError):
        FingerprintStore(tmp_path / "x.sqlite", app="a", synchronous="OFF")


def test_app_namespace_is_enforced(tmp_path):
    FingerprintStore(tmp_path / "fp.sqlite", app="trading").close()
    with pytest.raises(FingerprintStoreError):
        FingerprintStore(tmp_path / "fp.sqlite", app="purity")


def test_newer_schema_is_refused(tmp_path):
    p = tmp_path / "fp.sqlite"
    FingerprintStore(p, app="trading").close()
    c = sqlite3.connect(p)
    c.execute(f"PRAGMA user_version={fs.SCHEMA_VERSION + 1}")
    c.close()
    with pytest.raises(FingerprintSchemaTooNew):
        FingerprintStore(p, app="trading")


@pytest.mark.parametrize("kind", ["garbage", "truncated", "zero"])
def test_corrupt_database_fails_closed_and_is_never_replaced(tmp_path, tree, kind):
    p = tmp_path / "fp.sqlite"
    with FingerprintStore(p, app="trading") as s:
        settle(s, tree)
    if kind == "garbage":
        p.write_bytes(b"this is not a sqlite database" * 100)
    elif kind == "truncated":
        data = p.read_bytes()
        p.write_bytes(data[: len(data) // 3] + b"\x00" * 64)
    else:
        p.write_bytes(b"")
    for side in ("-wal", "-shm"):
        Path(str(p) + side).unlink(missing_ok=True)
    before = p.read_bytes()
    with pytest.raises(FingerprintStoreCorrupt):
        FingerprintStore(p, app="trading")
    assert p.read_bytes() == before                                                       # not recreated, not "fixed"


def test_quarantine_then_rebuild_reports_everything_dirty(tmp_path, tree):
    p = tmp_path / "fp.sqlite"
    with FingerprintStore(p, app="trading") as s:
        settle(s, tree)
    p.write_bytes(b"garbage" * 50)
    with pytest.raises(FingerprintStoreCorrupt):
        FingerprintStore(p, app="trading")
    moved = FingerprintStore.quarantine_corrupt(p)
    assert moved.exists() and not p.exists()
    with FingerprintStore(p, app="trading") as s:
        rep = s.reconcile(EDGE, tree)
        assert rep.new == 3 and s.counts(EDGE) == {"NEW": 3}
        assert s.dirty_set(EDGE) and not s.list_by_state(EDGE, CLEAN)                  # an empty store is never "clean"


# ---------------------------------------------------------------------------------------- record semantics
def test_lookup_record_verified_roundtrip_and_states(store):
    assert store.lookup(EDGE, "x") is None and store.state_of(EDGE, "x") == NEW
    store.record_verified(EDGE, "x", size=3, mtime_ns=5, content_hash="h", hash_algo="sha256")
    r = store.lookup(EDGE, "x")
    assert (r.state, r.size, r.mtime_ns, r.content_hash, r.hash_algo) == (CLEAN, 3, 5, "h", "sha256") and r.verified_at and r.last_seen_at
    store.mark_dirty(EDGE, "x")
    assert store.state_of(EDGE, "x") == DIRTY and store.lookup(EDGE, "x").content_hash == "h"
    store.mark_dirty(EDGE, "x", CONFLICT)
    assert store.state_of(EDGE, "x") == CONFLICT
    with pytest.raises(ValueError):
        store.mark_dirty(EDGE, "x", CLEAN)
    store.mark_dirty(EDGE, "ghost", UNKNOWN)
    assert store.lookup(EDGE, "ghost").state == UNKNOWN and store.lookup(EDGE, "ghost").size is None
    assert {r.path for r in store.dirty_set(EDGE)} == {"x", "ghost"}                    # UNKNOWN counts as dirty
    store.record_verified(EDGE, "x", size=3, mtime_ns=5, content_hash="h")              # explicit verified copy resolves the conflict
    assert store.state_of(EDGE, "x") == CLEAN


def test_edges_are_independent(store):
    store.record_verified("e1", "p", size=1, mtime_ns=1, content_hash="a")
    assert store.lookup("e2", "p") is None and store.state_of("e2", "p") == NEW


# ---------------------------------------------------------------------------------------- reconcile: stat-first
def test_first_reconcile_marks_new_without_hashing_or_copying(store, tree):
    h = Counter()
    rep = store.reconcile(EDGE, tree, hash_fn=h)
    assert (rep.scanned, rep.new, rep.hashed, rep.complete) == (3, 3, 0, True) and h.paths == []
    assert store.counts(EDGE) == {"NEW": 3}


def test_unchanged_repeat_run_hashes_nothing(store, tree):
    settle(store, tree)
    h = Counter()
    rep = store.reconcile(EDGE, tree, hash_fn=h)
    assert h.paths == [] and rep.hashed == 0 and rep.unchanged == 3 and rep.dirty == rep.new == rep.missing == 0
    assert store.counts(EDGE) == {"CLEAN": 3}


def test_touched_but_identical_refreshes_stat_and_stays_clean(store, tree):
    settle(store, tree)
    f = tree / "a" / "one.txt"
    old = store.lookup(EDGE, "a/one.txt")
    os.utime(f, ns=(old.mtime_ns + 10_000_000_000, old.mtime_ns + 10_000_000_000))
    h = Counter()
    rep = store.reconcile(EDGE, tree, hash_fn=h)
    assert h.paths == ["one.txt"] and rep.touched_identical == 1 and rep.dirty == 0
    r = store.lookup(EDGE, "a/one.txt")
    assert r.state == CLEAN and r.mtime_ns == old.mtime_ns + 10_000_000_000 and r.content_hash == old.content_hash
    h2 = Counter()
    store.reconcile(EDGE, tree, hash_fn=h2)
    assert h2.paths == []                                                               # fingerprint refreshed: no further hashing


def test_changed_content_becomes_dirty_and_is_not_silently_skipped(store, tree):
    settle(store, tree)
    (tree / "a" / "two.txt").write_bytes(b"two changed")
    rep = store.reconcile(EDGE, tree)
    r = store.lookup(EDGE, "a/two.txt")
    assert rep.dirty == 1 and r.state == DIRTY and r.content_hash == sha(b"two!")      # baseline = last VERIFIED hash, not the new content
    assert {x.path for x in store.dirty_set(EDGE)} == {"a/two.txt"}
    rep2 = store.reconcile(EDGE, tree, hash_fn=Counter())
    assert store.state_of(EDGE, "a/two.txt") == DIRTY and rep2.hashed == 0              # stays dirty until a verified copy
    (tree / "a" / "two.txt").write_bytes(b"two!")                                       # reverted to the verified content
    os.utime(tree / "a" / "two.txt", ns=(1, 1))
    store.reconcile(EDGE, tree)
    assert store.state_of(EDGE, "a/two.txt") == CLEAN


def test_new_file_and_missing_file_and_reappearance(store, tree):
    settle(store, tree)
    (tree / "b" / "four.txt").write_bytes(b"4")
    victim = tree / "a" / "one.txt"
    content = victim.read_bytes()
    victim.unlink()
    rep = store.reconcile(EDGE, tree)
    assert rep.new == 1 and rep.missing == 1
    assert store.state_of(EDGE, "b/four.txt") == NEW and store.state_of(EDGE, "a/one.txt") == MISSING
    victim.write_bytes(content)                                                         # comes back identical
    store.reconcile(EDGE, tree)
    assert store.state_of(EDGE, "a/one.txt") == CLEAN


def test_conflict_is_sticky_until_verified(store, tree):
    settle(store, tree)
    store.mark_dirty(EDGE, "a/one.txt", CONFLICT)
    (tree / "a" / "one.txt").write_bytes(b"other")
    store.reconcile(EDGE, tree)
    assert store.state_of(EDGE, "a/one.txt") == CONFLICT


def test_full_hash_catches_same_size_same_mtime_edit(store, tree):
    settle(store, tree)
    f = tree / "a" / "one.txt"
    st = f.stat()
    f.write_bytes(b"ONE")                                                               # same size
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))                                    # same mtime
    assert store.reconcile(EDGE, tree, hash_fn=Counter()).dirty == 0                    # cheap reconcile cannot see it (by design)
    rep = store.full_hash(EDGE, tree)
    assert rep.dirty == 1 and rep.same_stat_mismatch == ["a/one.txt"] and rep.hashed == 3
    assert store.state_of(EDGE, "a/one.txt") == DIRTY


def test_hash_error_makes_unknown_never_clean(store, tree):
    settle(store, tree)
    (tree / "a" / "one.txt").write_bytes(b"changed")

    def bad(p):
        raise PermissionError("locked")
    rep = store.reconcile(EDGE, tree, hash_fn=bad)
    assert store.state_of(EDGE, "a/one.txt") == UNKNOWN and not rep.complete and rep.errors
    assert not store.edge_status(EDGE)["complete"]


def test_unlistable_directory_is_unknown_not_missing(store, tree, monkeypatch):
    settle(store, tree)
    real = os.scandir

    def flaky(path="."):
        if os.fspath(path).replace("\\", "/").endswith("/b"):
            raise PermissionError("denied")
        return real(path)
    monkeypatch.setattr(os, "scandir", flaky)
    rep = store.reconcile(EDGE, tree)
    assert not rep.complete and rep.errors
    assert store.state_of(EDGE, "b/three.txt") == UNKNOWN                               # uncertainty: neither CLEAN nor MISSING
    assert store.state_of(EDGE, "a/one.txt") == CLEAN


def test_missing_or_unreadable_root_changes_nothing(store, tree, tmp_path):
    settle(store, tree)
    before = store.counts(EDGE)
    with pytest.raises(ReconcileError):
        store.reconcile(EDGE, tmp_path / "unmounted")                                   # e.g. a vanished volume must not mark everything MISSING
    with pytest.raises(ReconcileError):
        store.reconcile(EDGE, tree / "a" / "one.txt")
    assert store.counts(EDGE) == before


def test_imported_evidence_starts_unknown_then_stat_hash_once(store, tree):
    n = store.import_unknown(EDGE, [("a/one.txt", sha(b"one"), "sha256"), ("a/two.txt", sha(b"WRONG"), "sha256"), ("b/three.txt", None, None)])
    assert n == 3 and store.counts(EDGE) == {"UNKNOWN": 3}
    assert not store.list_by_state(EDGE, CLEAN)
    h = Counter()
    store.reconcile(EDGE, tree, hash_fn=h)
    assert sorted(h.paths) == ["one.txt", "three.txt", "two.txt"]                       # stat+hashed once
    assert store.state_of(EDGE, "a/one.txt") == CLEAN                                   # hash matched the imported evidence
    assert store.state_of(EDGE, "a/two.txt") == DIRTY                                   # mismatch
    assert store.state_of(EDGE, "b/three.txt") == DIRTY                                 # no baseline: never assumed clean
    h2 = Counter()
    store.reconcile(EDGE, tree, hash_fn=h2)
    assert h2.paths == []


# ---------------------------------------------------------------------------------------- folder state
def test_folder_state_derives_from_children_and_file_set(store, tree):
    assert store.folder_state(EDGE, "a").state == UNKNOWN                               # no rows: not clean
    settle(store, tree)
    assert store.folder_state(EDGE, "a").state == CLEAN and store.folder_state(EDGE).state == CLEAN
    (tree / "a" / "added.txt").write_bytes(b"+")
    store.reconcile(EDGE, tree)
    assert store.folder_state(EDGE, "a").state == DIRTY and store.folder_state(EDGE, "b").state == CLEAN
    (tree / "b" / "three.txt").unlink()
    store.reconcile(EDGE, tree)
    assert store.folder_state(EDGE, "b").state == DIRTY                                 # removed file set difference
    by = {f.path: f.state for f in store.folder_states(EDGE, depth=1)}
    assert by == {"a": DIRTY, "b": DIRTY}


def test_directory_mtime_is_not_used(store, tree):
    settle(store, tree)
    d = tree / "a"
    os.utime(d, ns=(5, 5))                                                              # directory mtime churn alone changes nothing
    assert store.reconcile(EDGE, tree, hash_fn=Counter()).hashed == 0
    assert store.counts(EDGE) == {"CLEAN": 3}


# ---------------------------------------------------------------------------------------- crash safety / read-only verify
class Boom(BaseException):
    pass


def test_interrupted_run_keeps_committed_batches_and_resumes(tmp_path):
    root = tmp_path / "big"
    root.mkdir()
    for i in range(30):
        (root / f"f{i:02d}.bin").write_bytes(b"x" * (i + 1))
    p = tmp_path / "fp.sqlite"
    with FingerprintStore(p, app="trading") as s:
        settle(s, root)
    for i in range(30):
        (root / f"f{i:02d}.bin").write_bytes(b"y" * (i + 2))                            # all change (size differs)
    calls = {"n": 0}

    def dying(path):
        calls["n"] += 1
        if calls["n"] == 12:
            raise Boom()
        return sha(Path(path).read_bytes())
    s = FingerprintStore(p, app="trading")
    with pytest.raises(Boom):
        s.reconcile(EDGE, root, hash_fn=dying, batch_size=5)
    s._db.close()                                                                       # simulate the process dying (no clean shutdown)
    with FingerprintStore(p, app="trading") as s2:
        st = s2.edge_status(EDGE)
        assert st["interrupted"] and not st["complete"]                                 # stale/interrupted is visible, never silently clean
        counts = s2.counts(EDGE)
        assert counts.get("DIRTY", 0) >= 10                                             # earlier committed batches survived
        rep = s2.reconcile(EDGE, root, batch_size=5)                                    # restart simply continues
        assert rep.complete and s2.counts(EDGE) == {"DIRTY": 30}
        assert not s2.edge_status(EDGE)["interrupted"] and s2.edge_status(EDGE)["complete"]


def test_verify_is_read_only_and_never_hashes(store, tree, monkeypatch):
    settle(store, tree)
    store.reconcile(EDGE, tree)
    (tree / "a" / "one.txt").write_bytes(b"changed!!")
    (tree / "b" / "three.txt").unlink()
    (tree / "b" / "extra.txt").write_bytes(b"e")

    def dump():
        return store._db.execute("SELECT * FROM fingerprints ORDER BY edge, path").fetchall(), store._db.execute("SELECT * FROM edge_meta ORDER BY 1,2").fetchall()
    before = dump()
    monkeypatch.setattr(fs, "sha256_file", lambda *a, **k: (_ for _ in ()).throw(AssertionError("verify must not hash")))
    rep = store.verify(EDGE, tree)
    assert dump() == before                                                             # nothing written
    assert rep.stat_changed == ["a/one.txt"] and rep.missing == ["b/three.txt"] and rep.new == ["b/extra.txt"] and not rep.clean
    assert {f.path: f.state for f in rep.folders} == {"a": CLEAN, "b": CLEAN}           # stored state only; drift reported separately


def test_verify_reports_stale_edge_as_not_clean(store, tree):
    s = store.verify(EDGE, tree)
    assert s.status["never_reconciled"] and not s.clean
    settle(store, tree)
    assert store.verify(EDGE, tree).clean


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are Windows-specific")
def test_junctions_are_not_followed(store, tree, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_bytes(b"s")
    link = tree / "linkdir"
    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True)
    if r.returncode != 0:
        pytest.skip("junction creation not permitted")
    rep = store.reconcile(EDGE, tree)
    assert rep.skipped_links >= 1 and store.lookup(EDGE, "linkdir/secret.txt") is None


# ---------------------------------------------------------------------------------------- independence
FORBIDDEN = ("trading_platform", "purity_app", "trading_library", "purity")


def test_new_code_imports_neither_application():
    src = Path(fs.__file__).read_text(encoding="utf-8")
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    assert not (mods & set(FORBIDDEN)), mods
    assert {m for m in mods if m == "shane_common"} == {"shane_common"}               # only its own package


def test_importing_the_store_pulls_in_no_application_module():
    code = ("import sys, shane_common.runtime.transfer.fingerprint_store as m;"
            "bad=[k for k in sys.modules if k.split('.')[0] in ('trading_platform','purity_app','trading_library')];"
            "print('BAD', bad) if bad else print('CLEAN')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert r.stdout.strip() == "CLEAN", (r.stdout, r.stderr)


def test_existing_copy_file_verified_is_untouched():
    import inspect

    from shane_common.runtime.transfer import copy as cp
    assert "fingerprint" not in inspect.getsource(cp)
