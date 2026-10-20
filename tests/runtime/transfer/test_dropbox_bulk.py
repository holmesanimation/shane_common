"""M5-4B: bulk Dropbox uploader against the in-memory double. tmp_path only; no network."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from dropbox.exceptions import ApiError, RateLimitError

from shane_common.runtime.transfer.dropbox_bulk import BulkConfig, BulkItem, BulkUploader, CountingClient, list_remote_tree
from shane_common.runtime.transfer.dropbox_destination import MIB, DropboxDestination, Throttle, UploadOutcome, dropbox_content_hash
from shane_common.runtime.transfer.dropbox_testing import FakeDropbox, not_found_error

PREFIX = "src/scope"


def make_files(root: Path, n: int, size: int = 100) -> list:
    out = []
    for i in range(n):
        p = root / f"f{i:04d}.bin"
        p.write_bytes((f"{i:04d}".encode() * (size // 4 + 1))[:size])
        out.append(BulkItem(f"f{i:04d}.bin", p, f"{PREFIX}/f{i:04d}.bin"))
    return out


def run(fake, items, cfg=None, cancel=None, **dkw):
    d = DropboxDestination(fake, remote_root="/bk", sleep=lambda s: None, throttle=Throttle(sleep=lambda s: None), **dkw)
    got: dict = {}
    up = BulkUploader(d, cfg or BulkConfig(workers=4))
    stats = up.store_many(items, on_result=lambda i, r: got.__setitem__(i.rel, r), should_cancel=cancel, list_prefix=PREFIX)
    return got, stats, d


def test_concurrent_small_files_verified_with_listing_and_few_calls(tmp_path):
    fake = FakeDropbox()
    items = make_files(tmp_path, 60)
    got, stats, _ = run(fake, items)
    assert len(got) == 60 and all(r.verified and r.outcome == UploadOutcome.VERIFIED_UPLOADED for r in got.values())
    for it in items:
        assert fake.files[("/bk/" + it.remote_rel).lower()][0] == it.local.read_bytes()
    assert fake.call_counts["files_upload"] == 60
    assert fake.call_counts["files_get_metadata"] == 0                       # pre-check and verification came from the recursive listings
    assert fake.call_counts["files_list_folder"] == 2                        # one before, one after
    assert stats.verified == 60 and stats.metadata_fallbacks == 0 and stats.listing_complete


def test_rerun_uploads_nothing(tmp_path):
    fake = FakeDropbox()
    items = make_files(tmp_path, 20)
    run(fake, items)
    before = fake.call_counts["files_upload"]
    got, stats, _ = run(fake, items)
    assert fake.call_counts["files_upload"] == before and stats.bytes_sent == 0
    assert all(r.outcome == UploadOutcome.ALREADY_PRESENT_VERIFIED and r.verified for r in got.values())


def test_pagination_retrieves_every_page(tmp_path):
    fake = FakeDropbox(list_page_size=7)
    items = make_files(tmp_path, 30)
    run(fake, items)
    d = DropboxDestination(fake, remote_root="/bk")
    listing = list_remote_tree(d, PREFIX)
    assert listing.complete and len(listing.entries) == 30 and listing.pages == 5
    assert fake.call_counts["files_list_folder_continue"] >= 4


def test_missing_prefix_is_a_complete_empty_listing(tmp_path):
    d = DropboxDestination(FakeDropbox(), remote_root="/bk")
    l = list_remote_tree(d, "nothing/here")
    assert l.complete and not l.entries


def test_listing_failure_midway_is_incomplete_and_never_proves_absence(tmp_path):
    fake = FakeDropbox(list_page_size=5)
    items = make_files(tmp_path, 12)
    run(fake, items[:6])
    fake.fail["files_list_folder_continue"] = [ApiError("r", "boom", "x", "en")] * 4
    d = DropboxDestination(fake, remote_root="/bk", sleep=lambda s: None)
    l = list_remote_tree(d, PREFIX)
    assert not l.complete and l.error
    # an incomplete pre-listing must not make 'absent from listing' authoritative: the metadata lookup decides (here: already present, hash equal -> nothing uploaded)
    fake.fail["files_list_folder_continue"] = [ApiError("r", "boom", "x", "en")] * 4
    fake.call_counts.clear()
    got, stats, _ = run(fake, items[:6])
    assert all(r.outcome == UploadOutcome.ALREADY_PRESENT_VERIFIED for r in got.values()) and fake.call_counts["files_upload"] == 0
    assert stats.metadata_fallbacks >= 1 and not stats.listing_complete


def test_listing_lag_before_upload_falls_back_to_metadata_and_never_overwrites(tmp_path):
    fake = FakeDropbox()
    items = make_files(tmp_path, 5)
    run(fake, items)
    fake.hide_from_listing = {("/bk/" + i.remote_rel).lower() for i in items}      # present remotely, invisible in listings
    fake.call_counts.clear()
    got, _, _ = run(fake, items)
    assert all(r.outcome == UploadOutcome.ALREADY_PRESENT_VERIFIED and r.verified for r in got.values())
    assert fake.call_counts["files_upload"] == 5                                    # attempted as add -> server said conflict -> metadata decided, nothing written
    assert fake.call_counts["files_get_metadata"] == 5


def test_post_upload_listing_lag_uses_targeted_metadata_before_judging(tmp_path):
    fake = FakeDropbox()
    items = make_files(tmp_path, 6)
    fake.hide_from_listing = {("/bk/" + i.remote_rel).lower() for i in items}      # uploads succeed but do not show in the listing yet
    got, stats, _ = run(fake, items)
    assert all(r.verified for r in got.values()) and fake.call_counts["files_get_metadata"] == 6 and stats.metadata_fallbacks == 6


def test_unverified_when_neither_listing_nor_metadata_establish_the_hash(tmp_path):
    class Lossy(FakeDropbox):
        def files_get_metadata(self, path, **kw):
            self._maybe_fail("files_get_metadata")
            raise not_found_error()
    fake = Lossy()
    items = make_files(tmp_path, 3)
    fake.hide_from_listing = {("/bk/" + i.remote_rel).lower() for i in items}
    got, _, _ = run(fake, items)
    assert {r.outcome for r in got.values()} == {UploadOutcome.UNVERIFIED} and not any(r.verified for r in got.values())


def test_conflict_is_never_overwritten_but_recorded_hash_allows_refresh(tmp_path):
    fake = FakeDropbox()
    items = make_files(tmp_path, 2)
    d0 = DropboxDestination(fake, remote_root="/bk")
    fake.files_upload(b"someone else", "/bk/" + items[0].remote_rel)
    fake.files_upload(b"our old bytes", "/bk/" + items[1].remote_rel)
    old = fake.files_get_metadata("/bk/" + items[1].remote_rel).content_hash
    items[1] = BulkItem(items[1].rel, items[1].local, items[1].remote_rel, recorded_remote_hash=old)
    got, _, _ = run(fake, items)
    assert got[items[0].rel].outcome == UploadOutcome.CONFLICT and fake.files[("/bk/" + items[0].remote_rel).lower()][0] == b"someone else"
    assert got[items[1].rel].outcome == UploadOutcome.REFRESHED_VERIFIED and got[items[1].rel].verified


def test_corrupted_transfer_is_a_hash_mismatch(tmp_path):
    fake = FakeDropbox()
    fake.corrupt_next_upload = True
    got, _, _ = run(fake, make_files(tmp_path, 3))
    assert sorted(r.outcome.value for r in got.values()) == ["HASH_MISMATCH", "VERIFIED_UPLOADED", "VERIFIED_UPLOADED"]


def test_source_changed_during_transfer_is_not_verified(tmp_path):
    fake = FakeDropbox()
    items = make_files(tmp_path, 3)
    target = items[1].local

    def mutate(path):
        if path.endswith("f0001.bin"):
            st = os.stat(target)
            os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    fake.on_upload = mutate
    got, _, _ = run(fake, items)
    assert got["f0001.bin"].outcome == UploadOutcome.SOURCE_CHANGED and not got["f0001.bin"].verified
    assert got["f0000.bin"].verified and got["f0002.bin"].verified


def test_rate_limit_is_retried_with_shared_backoff(tmp_path):
    fake = FakeDropbox()
    fake.fail["files_upload"] = [RateLimitError("r", None, 1), RateLimitError("r", None, 1)]
    got, stats, d = run(fake, make_files(tmp_path, 10))
    assert all(r.verified for r in got.values()) and stats.rate_limits == 2 and stats.retries == 2


def test_retries_are_bounded(tmp_path):
    fake = FakeDropbox()
    fake.fail["files_upload"] = [RateLimitError("r", None, 1)] * 50
    got, _, _ = run(fake, make_files(tmp_path, 1), max_attempts=3)
    assert [r.outcome for r in got.values()] == [UploadOutcome.FAILED] and not list(got.values())[0].verified


def test_cancel_stops_submitting_and_claims_nothing_unverified(tmp_path):
    fake = FakeDropbox(latency=0.001)
    items = make_files(tmp_path, 60)
    stop = {"v": False}
    n = {"up": 0}

    def count(path):
        n["up"] += 1
        stop["v"] = n["up"] >= 5
    fake.on_upload = count
    got, stats, _ = run(fake, items, BulkConfig(workers=2), cancel=lambda: stop["v"])
    assert 5 <= len(fake.files) < 60
    assert not any(r.verified for r in got.values())                                  # uploaded but not independently verified: nothing is claimed
    assert all(r.outcome == UploadOutcome.CANCELLED for r in got.values())
    uploaded = len(fake.files)
    fake.on_upload = None
    fake.call_counts.clear()
    got, _, _ = run(fake, items)                                                       # restart: finishes the rest, re-sends nothing already present
    assert all(r.verified for r in got.values()) and fake.call_counts["files_upload"] == 60 - uploaded


def test_batch_commit_mode_verifies_and_groups_commits(tmp_path):
    fake = FakeDropbox()
    items = make_files(tmp_path, 25)
    got, stats, _ = run(fake, items, BulkConfig(workers=4, commit_mode="batch", batch_size=10))
    assert all(r.verified for r in got.values()) and len(got) == 25
    assert fake.call_counts["files_upload"] == 0 and fake.call_counts["files_upload_session_append_v2"] == 25
    assert fake.call_counts["files_upload_session_finish_batch_v2"] == stats.batch_commits <= 4
    assert fake.call_counts["files_upload_session_start_batch"] <= 4


def test_batch_commit_conflict_is_not_overwritten(tmp_path):
    fake = FakeDropbox()
    items = make_files(tmp_path, 3)
    fake.files_upload(b"x", "/bk/" + items[0].remote_rel)
    fake.hide_from_listing = {("/bk/" + items[0].remote_rel).lower()}
    got, _, _ = run(fake, items, BulkConfig(workers=2, commit_mode="batch"))
    assert got[items[0].rel].outcome == UploadOutcome.CONFLICT and fake.files[("/bk/" + items[0].remote_rel).lower()][0] == b"x"


def test_large_file_uses_the_session_path_and_is_verified(tmp_path):
    fake = FakeDropbox()
    big = tmp_path / "big.bin"
    big.write_bytes(os.urandom(5 * MIB))
    small = make_files(tmp_path, 2)
    item = BulkItem("big.bin", big, f"{PREFIX}/big.bin")
    got, _, _ = run(fake, small + [item], BulkConfig(workers=2, small_file_limit=MIB), single_shot_limit=MIB, chunk_size=4 * MIB)
    assert got["big.bin"].verified and got["big.bin"].resumed is False
    assert fake.files[("/bk/" + item.remote_rel).lower()][0] == big.read_bytes()


def test_backpressure_bounds_inflight_bytes(tmp_path):
    fake = FakeDropbox(latency=0.002)
    items = make_files(tmp_path, 30, size=600_000)
    cfg = BulkConfig(workers=8, max_inflight_bytes=1_300_000)
    got, stats, _ = run(fake, items, cfg)
    assert all(r.verified for r in got.values()) and stats.peak_inflight_bytes <= 1_300_000 and stats.peak_inflight_files <= 3


def test_case_insensitive_collision_is_reported_not_uploaded(tmp_path):
    fake = FakeDropbox()
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    a.write_bytes(b"1"), b.write_bytes(b"2")
    items = [BulkItem("A.bin", a, f"{PREFIX}/A.bin"), BulkItem("a.bin", b, f"{PREFIX}/a.bin")]
    got, _, _ = run(fake, items)
    assert {r.outcome for r in got.values()} == {UploadOutcome.CONFLICT} and not fake.files


def test_unreadable_and_missing_sources(tmp_path):
    fake = FakeDropbox()
    got, _, _ = run(fake, [BulkItem("gone.bin", tmp_path / "gone.bin", f"{PREFIX}/gone.bin")])
    assert got["gone.bin"].outcome == UploadOutcome.SOURCE_UNREADABLE


def test_counting_client_counts_by_method(tmp_path):
    fake = FakeDropbox()
    c = CountingClient(fake)
    d = DropboxDestination(c, remote_root="/bk", sleep=lambda s: None)
    BulkUploader(d, BulkConfig(workers=2)).store_many(make_files(tmp_path, 4), on_result=lambda i, r: None, list_prefix=PREFIX)
    assert c.counts["files_upload"] == 4 and c.counts["files_list_folder"] == 2


def test_shared_ledger_progress_is_safe_under_concurrent_workers(tmp_path):
    import threading
    from shane_common.runtime.transfer.dropbox_destination import LedgerProgress
    p = LedgerProgress(tmp_path / "sessions.json")
    errs = []

    def work(i):
        try:
            for j in range(15):
                p.put(f"k{i}-{j}", {"offset": j})
                p.get(f"k{i}-{j}")
                p.delete(f"k{i}-{j}")
        except Exception as e:                                     # noqa: BLE001
            errs.append(e)
    ts = [threading.Thread(target=work, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and p._l.load() == {}


def test_progress_store_io_error_is_a_failure_not_an_unreadable_source(tmp_path):
    from shane_common.runtime.transfer.dropbox_destination import UploadOutcome as O

    class Broken:
        def get(self, key): return None
        def put(self, key, value): raise FileNotFoundError("progress tmp vanished")
        def delete(self, key): pass
    f = tmp_path / "big.bin"
    f.write_bytes(os.urandom(5 * MIB))
    d = DropboxDestination(FakeDropbox(), remote_root="/bk", sleep=lambda s: None, single_shot_limit=MIB, chunk_size=4 * MIB, progress=Broken())
    r = d.store_file(f, "x/big.bin")
    assert r.outcome == O.FAILED and not r.verified


def test_reconcile_drops_rows_of_files_that_became_excluded(tmp_path):
    from shane_common.runtime.transfer.fingerprint_store import CLEAN, FingerprintStore
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.txt").write_text("a")
    (root / "b.txt").write_text("b")
    s = FingerprintStore(tmp_path / "fp.sqlite", app="t")
    s.reconcile("e", root)
    assert s.counts("e").get("NEW", 0) == 2
    s.reconcile("e", root, skip=lambda rel, is_dir: (not is_dir) and rel == "b.txt")
    assert s.lookup("e", "b.txt") is None and s.lookup("e", "a.txt") is not None and s.counts("e").get("MISSING", 0) == 0
    (root / "a.txt").unlink()
    s.reconcile("e", root, skip=lambda rel, is_dir: (not is_dir) and rel == "b.txt")
    assert s.counts("e").get("MISSING") == 1                             # a genuinely deleted, non-excluded file is still MISSING
