"""M5-5 P1: deterministic ZIP writer and hardened extractor (local only; tmp_path)."""

from __future__ import annotations

import hashlib
import os
import random
import shutil
import stat
import zipfile
from pathlib import Path

import pytest

from shane_common.runtime.transfer.deterministic_zip import StagingBudget, StagingBudgetExceeded, ZipSource, write_deterministic_zip
from shane_common.runtime.transfer.safe_extract import (ArchiveHashMismatch, ExtractLimits, NameRegistry, UnsafeArchive, extract_zip, prepare_target, sha256_file,
                                                        validate_rel)


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def make_files(root: Path, n=30, seed=1):
    rnd = random.Random(seed)
    files = {}
    for i in range(n):
        rel = f"S/lin/bars/{'5m' if i % 2 else '1d'}/p{i:03d}/{'bars.parquet' if i % 3 else 'partition.json'}"
        data = (b"abc" * rnd.randint(1, 400)) + bytes(rnd.getrandbits(8) for _ in range(rnd.randint(0, 200)))
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        files[rel] = data
    return files


def srcs(root: Path, files, deflate=True):
    return [ZipSource(rel, str(root / rel), deflate) for rel in files]


def manifest(res):
    return [{"path": e["path"], "size": e["size"], "sha256": e["sha256"], "mtime_ns": e["mtime_ns"]} for e in res.entries]


# ---------------------------------------------------------------------------------------------------------------- determinism
def test_identical_bytes_for_any_input_order_and_repeats(tmp_path):
    src = tmp_path / "src"
    files = make_files(src)
    shas = set()
    for k in range(3):
        items = srcs(src, files)
        random.Random(k).shuffle(items)
        r = write_deterministic_zip(items, tmp_path / f"o{k}.zip")
        shas.add(r.sha256)
        assert r.size == (tmp_path / f"o{k}.zip").stat().st_size
    assert len(shas) == 1


def test_cold_and_warm_runs_equal_and_mtime_does_not_leak_into_bytes(tmp_path):
    src = tmp_path / "src"
    files = make_files(src)
    a = write_deterministic_zip(srcs(src, files), tmp_path / "a.zip")
    for rel in files:
        os.utime(src / rel, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    b = write_deterministic_zip(srcs(src, files), tmp_path / "b.zip")
    assert a.sha256 == b.sha256 and a.dropbox_hash == b.dropbox_hash


def test_entries_record_exact_hashes_and_deflate_compresses(tmp_path):
    src = tmp_path / "src"
    files = make_files(src)
    r = write_deterministic_zip(srcs(src, files), tmp_path / "a.zip")
    assert {e["path"]: e["sha256"] for e in r.entries} == {k: sha(v) for k, v in files.items()}
    assert sum(e["stored_size"] for e in r.entries) < sum(e["size"] for e in r.entries)
    with zipfile.ZipFile(tmp_path / "a.zip") as zf:
        assert all(zi.date_time == (1980, 1, 1, 0, 0, 0) and zi.compress_type == zipfile.ZIP_DEFLATED for zi in zf.infolist())
        assert zf.comment == b"" and all(zi.extra == b"" for zi in zf.infolist())
        assert [zi.filename for zi in zf.infolist()] == sorted(files, key=lambda s: s.encode())


def test_level_and_stored_mode_change_bytes_but_stay_deterministic(tmp_path):
    src = tmp_path / "src"
    files = make_files(src)
    l6 = write_deterministic_zip(srcs(src, files), tmp_path / "l6.zip", compresslevel=6)
    l6b = write_deterministic_zip(srcs(src, files), tmp_path / "l6b.zip", compresslevel=6)
    l1 = write_deterministic_zip(srcs(src, files), tmp_path / "l1.zip", compresslevel=1)
    st = write_deterministic_zip(srcs(src, files, deflate=False), tmp_path / "st.zip")
    assert l6.sha256 == l6b.sha256 and len({l6.sha256, l1.sha256, st.sha256}) == 3
    assert st.size > l6.size


def test_duplicate_names_and_bad_level_refused(tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"1")
    with pytest.raises(ValueError):
        write_deterministic_zip([ZipSource("a", str(f)), ZipSource("a", str(f))], tmp_path / "o.zip")
    with pytest.raises(ValueError):
        write_deterministic_zip([ZipSource("a", str(f))], tmp_path / "o.zip", compresslevel=0)


def test_sources_opened_read_only_and_unchanged(tmp_path):
    src = tmp_path / "src"
    files = make_files(src)
    before = {r: ((src / r).stat().st_mtime_ns, sha256_file(src / r)) for r in files}
    write_deterministic_zip(srcs(src, files), tmp_path / "a.zip")
    assert before == {r: ((src / r).stat().st_mtime_ns, sha256_file(src / r)) for r in files}


def test_unstable_member_is_flagged_and_failed_build_leaves_no_partial(tmp_path, monkeypatch):
    src = tmp_path / "src"
    files = make_files(src, 5)
    first = sorted(files)[0]
    real_stat = os.stat
    calls = {"n": 0}

    def flaky(p, *a, **k):
        st = real_stat(p, *a, **k)
        if str(p).endswith(first.replace("/", os.sep)):
            calls["n"] += 1
            if calls["n"] == 2:                                  # the post-read stat: pretend mtime moved
                class S:
                    st_size, st_mtime_ns = st.st_size, st.st_mtime_ns + 1
                return S()
        return st

    monkeypatch.setattr(os, "stat", flaky)
    r = write_deterministic_zip(srcs(src, files), tmp_path / "a.zip")
    monkeypatch.undo()
    assert r.unstable == [first] and [e["stable"] for e in r.entries].count(False) == 1
    with pytest.raises(FileNotFoundError):
        write_deterministic_zip([ZipSource("gone", str(tmp_path / "missing"))], tmp_path / "b.zip")
    assert not (tmp_path / "b.zip").exists() and not (tmp_path / "b.zip.partial").exists()


def test_staging_budget_enforced(tmp_path):
    src = tmp_path / "src"
    files = make_files(src, 5)
    with pytest.raises(StagingBudgetExceeded):
        write_deterministic_zip(srcs(src, files), tmp_path / "a.zip", budget=StagingBudget(tmp_path / "stg", limit=1024))
    b = StagingBudget(tmp_path / "stg2", limit=300 * 1024 * 1024)
    write_deterministic_zip(srcs(src, files), tmp_path / "stg2" / "a.zip", budget=b)
    assert b.peak > 0


# ---------------------------------------------------------------------------------------------------------------- extraction
def build(tmp_path, n=20):
    src = tmp_path / "src"
    files = make_files(src, n)
    r = write_deterministic_zip(srcs(src, files), tmp_path / "pkg.zip")
    return src, files, r, tmp_path / "pkg.zip"


def test_roundtrip_exact_and_hash_verified(tmp_path):
    src, files, r, z = build(tmp_path)
    t = prepare_target(tmp_path / "restore")
    out = extract_zip(z, manifest(r), t, expected_sha256=r.sha256)
    assert out["extracted"] == len(files)
    for rel, data in files.items():
        assert (t / rel).read_bytes() == data
        assert (t / rel).stat().st_mtime_ns == (src / rel).stat().st_mtime_ns
    assert not (t / ".restore_tmp").exists()


def test_wanted_subset(tmp_path):
    _, files, r, z = build(tmp_path)
    t = prepare_target(tmp_path / "restore")
    out = extract_zip(z, manifest(r), t, wanted=lambda n: "/1d/" in n)
    assert 0 < out["extracted"] < len(files)
    assert all("/1d/" in str(p).replace("\\", "/") for p in t.rglob("*") if p.is_file())


def test_archive_hash_checked_before_open_and_target_untouched(tmp_path):
    _, _, r, z = build(tmp_path)
    t = prepare_target(tmp_path / "restore")
    with pytest.raises(ArchiveHashMismatch):
        extract_zip(z, manifest(r), t, expected_sha256="0" * 64)
    assert not any(t.iterdir())


def test_flipped_byte_anywhere_never_yields_a_final_file(tmp_path):
    _, files, r, z = build(tmp_path, 8)
    raw = bytearray(z.read_bytes())
    rnd = random.Random(7)
    for k in range(25):
        b = bytearray(raw)
        b[rnd.randrange(len(b))] ^= 0xFF
        zp = tmp_path / f"bad{k}.zip"
        zp.write_bytes(bytes(b))
        t = tmp_path / f"r{k}"
        with pytest.raises(UnsafeArchive):
            extract_zip(zp, manifest(r), prepare_target(t), expected_sha256=r.sha256)
        assert not any(p.is_file() for p in t.rglob("*"))


def test_corrupt_member_without_package_hash_still_all_or_nothing(tmp_path):
    _, files, r, z = build(tmp_path, 6)
    m = manifest(r)
    m[3] = dict(m[3], sha256="0" * 64)                                         # one lying manifest hash
    t = prepare_target(tmp_path / "restore")
    with pytest.raises(UnsafeArchive, match="hash mismatch"):
        extract_zip(z, m, t)
    assert not any(p.is_file() for p in t.rglob("*"))


def make_zip(path: Path, entries, comp=zipfile.ZIP_DEFLATED):
    with zipfile.ZipFile(path, "w", compression=comp) as zf:
        for name, data, attr in entries:
            zi = zipfile.ZipInfo(name)
            zi.compress_type = comp
            if attr is not None:
                zi.external_attr = attr
            zf.writestr(zi, data)


def decl(name, data):
    return {"path": name, "size": len(data), "sha256": sha(data)}


HOSTILE = ["../evil", "a/../../evil", "/abs/x", "C:/x", "c:evil", "a\\b", "x:stream", "a/CON", "con.txt", "a/NUL.dat", "COM1", "lpt9.x", "a/b.", "a/b ", "a//b", "./a", "a/./b",
           "a/b\x00c", "a/b\x1fc", "a/q?z", "a/q*z", "a/q<z", "a/q|z", 'a/q"z', "COM¹", "CONIN$", "e\u0301.txt", "x" * 300]


@pytest.mark.parametrize("name", HOSTILE)
def test_hostile_names_rejected_by_validator(name):
    with pytest.raises(UnsafeArchive):
        validate_rel(name)


@pytest.mark.parametrize("name", ["IBKR/equities/1810/derived__aggregated_local/bars/5m/partitions/2026-01-01/bars.parquet", "a/b/c.json", "x_y-z/é.txt"])
def test_good_names_accepted(name):
    validate_rel(name)


@pytest.mark.parametrize("name", [n for n in HOSTILE if "\x00" not in n and "\x1f" not in n][:14])
def test_hostile_names_inside_archive_refused_and_nothing_written(tmp_path, name):
    z = tmp_path / "h.zip"
    make_zip(z, [("ok/a.txt", b"1", None), (name, b"2", None)])
    t = prepare_target(tmp_path / "r")
    with pytest.raises(UnsafeArchive):
        extract_zip(z, [decl("ok/a.txt", b"1"), decl(name, b"2")], t)
    assert not any(p.is_file() for p in t.rglob("*")) and not (tmp_path / "evil").exists()


def test_symlink_and_directory_and_device_entries_refused(tmp_path):
    for k, (attr, nm) in enumerate([(stat.S_IFLNK << 16, "ln"), (stat.S_IFCHR << 16, "dev"), (stat.S_IFDIR << 16, "d")]):
        z = tmp_path / f"s{k}.zip"
        make_zip(z, [(nm, b"target", attr)])
        with pytest.raises(UnsafeArchive):
            extract_zip(z, [decl(nm, b"target")], prepare_target(tmp_path / f"r{k}"))
    z = tmp_path / "dir.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("adir/", b"")
    with pytest.raises(UnsafeArchive):
        extract_zip(z, [], prepare_target(tmp_path / "rd"))


def test_encrypted_flag_refused(tmp_path):
    z = tmp_path / "e.zip"
    make_zip(z, [("a", b"1", None)])
    raw = bytearray(z.read_bytes())
    i = raw.index(b"PK\x01\x02")
    raw[i + 8] |= 1                                                           # central-directory general-purpose flag bit 0
    z.write_bytes(bytes(raw))
    with pytest.raises(UnsafeArchive):
        extract_zip(z, [decl("a", b"1")], prepare_target(tmp_path / "r"))


def test_duplicate_and_case_collision_members_refused(tmp_path):
    z = tmp_path / "d.zip"
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        make_zip(z, [("a/x", b"1", None), ("a/x", b"2", None)])
    with pytest.raises(UnsafeArchive, match="duplicate"):
        extract_zip(z, [decl("a/x", b"1")], prepare_target(tmp_path / "r1"))
    z2 = tmp_path / "c.zip"
    make_zip(z2, [("A/x", b"1", None), ("a/X", b"2", None)])
    with pytest.raises(UnsafeArchive, match="collision"):
        extract_zip(z2, [decl("A/x", b"1"), decl("a/X", b"2")], prepare_target(tmp_path / "r2"))


def test_collision_detected_across_packages_with_shared_registry(tmp_path):
    z1, z2 = tmp_path / "1.zip", tmp_path / "2.zip"
    make_zip(z1, [("A/x", b"1", None)])
    make_zip(z2, [("a/X", b"2", None)])
    t, reg = prepare_target(tmp_path / "r"), NameRegistry()
    extract_zip(z1, [decl("A/x", b"1")], t, registry=reg)
    with pytest.raises(UnsafeArchive, match="collision"):
        extract_zip(z2, [decl("a/X", b"2")], t, registry=reg)


def test_file_directory_conflict(tmp_path):
    r = NameRegistry()
    r.add("a/b")
    with pytest.raises(UnsafeArchive):
        r.add("a/b/c")
    r2 = NameRegistry()
    r2.add("a/b/c")
    with pytest.raises(UnsafeArchive):
        r2.add("a/b")


def test_undeclared_missing_size_mismatch_and_oversize(tmp_path):
    z = tmp_path / "u.zip"
    make_zip(z, [("a", b"1", None), ("b", b"22", None)])
    with pytest.raises(UnsafeArchive, match="not in manifest"):
        extract_zip(z, [decl("a", b"1")], prepare_target(tmp_path / "r1"))
    with pytest.raises(UnsafeArchive, match="missing"):
        extract_zip(z, [decl("a", b"1"), decl("b", b"22"), decl("c", b"3")], prepare_target(tmp_path / "r2"))
    with pytest.raises(UnsafeArchive, match="size"):
        extract_zip(z, [decl("a", b"1"), dict(decl("b", b"22"), size=5)], prepare_target(tmp_path / "r3"))
    with pytest.raises(UnsafeArchive, match="oversize"):
        extract_zip(z, [decl("a", b"1"), decl("b", b"22")], prepare_target(tmp_path / "r4"), limits=ExtractLimits(max_member_bytes=1))


def test_member_count_total_and_ratio_limits(tmp_path):
    z = tmp_path / "l.zip"
    make_zip(z, [(f"f{i}", b"x", None) for i in range(10)])
    m = [decl(f"f{i}", b"x") for i in range(10)]
    with pytest.raises(UnsafeArchive, match="too many"):
        extract_zip(z, m, prepare_target(tmp_path / "r1"), limits=ExtractLimits(max_members=5))
    with pytest.raises(UnsafeArchive, match="total"):
        extract_zip(z, m, prepare_target(tmp_path / "r2"), limits=ExtractLimits(max_total_bytes=5))
    bomb = tmp_path / "bomb.zip"
    data = b"\0" * (40 * 1024 * 1024)
    make_zip(bomb, [("z", data, None)])
    with pytest.raises(UnsafeArchive, match="ratio"):
        extract_zip(bomb, [decl("z", data)], prepare_target(tmp_path / "r3"))


def test_lying_header_size_cannot_overrun(tmp_path):
    z = tmp_path / "lie.zip"
    make_zip(z, [("a", b"hello world", None)])
    raw = bytearray(z.read_bytes())
    # shrink the central-directory uncompressed size so the declared size no longer matches the manifest
    i = raw.index(b"PK\x01\x02")
    raw[i + 24:i + 28] = (3).to_bytes(4, "little")
    z.write_bytes(bytes(raw))
    with pytest.raises(UnsafeArchive):
        extract_zip(z, [decl("a", b"hello world")], prepare_target(tmp_path / "r"))


def test_never_overwrites_and_target_must_be_new_or_empty(tmp_path):
    _, files, r, z = build(tmp_path, 4)
    t = tmp_path / "busy"
    t.mkdir()
    (t / "keep.txt").write_text("x")
    with pytest.raises(UnsafeArchive, match="new or empty"):
        prepare_target(t)
    t2 = prepare_target(tmp_path / "ok")
    first = sorted(files)[0]
    (t2 / first).parent.mkdir(parents=True)
    (t2 / first).write_bytes(b"pre-existing")
    with pytest.raises(UnsafeArchive):
        extract_zip(z, manifest(r), t2)
    assert (t2 / first).read_bytes() == b"pre-existing"


def test_forbidden_roots_block_in_place_restore(tmp_path):
    canon = tmp_path / "TP_DATA" / "marketdata"
    canon.mkdir(parents=True)
    for target in (canon, canon / "IBKR", tmp_path / "TP_DATA"):
        with pytest.raises(UnsafeArchive, match="protected"):
            prepare_target(target, forbidden_roots=(canon,))
    prepare_target(tmp_path / "elsewhere", forbidden_roots=(canon,))


def test_path_length_and_free_space(tmp_path):
    z = tmp_path / "p.zip"
    make_zip(z, [("a/b/c.txt", b"1", None)])
    with pytest.raises(UnsafeArchive, match="exceeds"):
        extract_zip(z, [decl("a/b/c.txt", b"1")], prepare_target(tmp_path / "r1"), limits=ExtractLimits(max_abs_len=10))
    free = shutil.disk_usage(tmp_path).free
    with pytest.raises(UnsafeArchive, match="free space"):
        extract_zip(z, [decl("a/b/c.txt", b"1")], prepare_target(tmp_path / "r2"), limits=ExtractLimits(min_free_bytes=free + 1))


def test_not_a_zip_and_truncated_zip(tmp_path):
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip at all")
    with pytest.raises(UnsafeArchive):
        extract_zip(bad, [], prepare_target(tmp_path / "r"))
    _, _, r, z = build(tmp_path, 5)
    cut = tmp_path / "cut.zip"
    cut.write_bytes(z.read_bytes()[:-40])
    with pytest.raises(UnsafeArchive):
        extract_zip(cut, manifest(r), prepare_target(tmp_path / "r2"))


def test_seeded_name_fuzz_validator_never_accepts_traversal_or_reserved():
    rnd = random.Random(2026)
    alphabet = ["a", "B", ".", "..", "/", "\\", ":", " ", "CON", "nul", "com3", "é", "x" * 5, "*", "?", "\x01"]
    for _ in range(4000):
        name = "".join(rnd.choice(alphabet) for _ in range(rnd.randint(1, 8)))
        try:
            validate_rel(name)
        except UnsafeArchive:
            continue
        segs = name.split("/")
        assert not name.startswith("/") and ".." not in segs and "." not in segs and "" not in segs
        assert "\\" not in name and ":" not in name and not any(c in name for c in '*?<>|"\x01')
        assert all(not s.endswith((".", " ")) and s.split(".")[0].upper() not in {"CON", "NUL", "COM3"} for s in segs)
