"""M5-5 P3: add-only bytes, strict marker creation, verified reads and range reads. FakeDropbox only - no live calls."""

from __future__ import annotations

import threading
from collections import Counter

import pytest
from dropbox.exceptions import ApiError
from dropbox.files import UploadError, UploadWriteFailed, WriteConflictError, WriteError, WriteMode

from shane_common.runtime.transfer import dropbox_objects as do
from shane_common.runtime.transfer.dropbox_destination import DropboxDestination
from shane_common.runtime.transfer.dropbox_testing import FakeDropbox


class SpyFake(FakeDropbox):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.upload_kwargs: list = []
        self.lose_response_once = False

    def files_upload(self, data, path, mode=None, mute=False, **kw):
        self.upload_kwargs.append((path, mode, kw))
        r = super().files_upload(data, path, mode=mode, mute=mute, **kw)
        if self.lose_response_once:
            self.lose_response_once = False
            raise ConnectionError("response lost after the write")
        return r


def make(atomic=True, **kw):
    fake = SpyFake(**kw)
    fake.atomic_add = atomic
    return fake, DropboxDestination(fake, remote_root="/ns", sleep=lambda s: None)


# ------------------------------------------------------------------------------------------------------------ markers
def test_marker_uses_add_mode_without_autorename_and_never_overwrites():
    fake, dest = make()
    r = do.store_marker(dest, b'{"n":1}', "m/C1.json")
    assert r.outcome is do.MarkerOutcome.WON and r.revisions == 1
    path, mode, kw = fake.upload_kwargs[-1]
    assert mode.is_add() and kw.get("autorename") is False
    assert do.store_marker(dest, b'{"n":1}', "m/C1.json").outcome is do.MarkerOutcome.EXISTS_SAME
    assert do.store_marker(dest, b'{"n":2}', "m/C1.json").outcome is do.MarkerOutcome.EXISTS_DIFFERENT
    assert fake.files["/ns/m/c1.json"][0] == b'{"n":1}' and fake.history["/ns/m/c1.json"].__len__() == 1
    assert all(m.is_add() for _, m, _ in fake.upload_kwargs)
    assert not any(isinstance(m, type(WriteMode.overwrite)) and m.is_overwrite() for _, m, _ in fake.upload_kwargs)


def test_marker_on_a_folder_is_a_conflict():
    fake, dest = make()
    fake.folders.add("/ns/m/c1.json")
    assert do.store_marker(dest, b"x", "m/C1.json").outcome is do.MarkerOutcome.EXISTS_DIFFERENT


def test_marker_payload_limits():
    _, dest = make()
    for bad in (b"", b"x" * (do.MAX_MARKER_BYTES + 1)):
        with pytest.raises(ValueError):
            do.store_marker(dest, bad, "m/C1.json")


def test_non_conflict_api_error_is_unknown_not_a_conflict():
    fake, dest = make()
    err = ApiError("r", UploadError.path(UploadWriteFailed(WriteError.insufficient_space, "s")), "x", "en")
    fake.fail["files_upload"] = [err]
    with pytest.raises(do.MarkerUnknown):
        do.store_marker(dest, b"x", "m/C1.json")
    assert "/ns/m/c1.json" not in fake.files


def test_is_conflict_understands_real_sdk_errors():
    real = ApiError("r", UploadError.path(UploadWriteFailed(WriteError.conflict(WriteConflictError.file), "s")), "x", "en")
    assert do.is_conflict(real)
    assert not do.is_conflict(ApiError("r", UploadError.path(UploadWriteFailed(WriteError.no_write_permission, "s")), "x", "en"))
    assert not do.is_conflict(ApiError("r", UploadError.payload_too_large, "x", "en"))


def test_lost_response_after_the_write_reads_back_as_exists_same():
    fake, dest = make()
    fake.lose_response_once = True
    r = do.store_marker(dest, b'{"n":1}', "m/C1.json")
    assert r.outcome is do.MarkerOutcome.EXISTS_SAME                 # the automatic retry hit the existing identical bytes: honest, not "WON"
    assert len(fake.history["/ns/m/c1.json"]) == 1


def test_lost_request_before_the_write_is_retried_to_a_win():
    fake, dest = make()
    fake.fail["files_upload"] = [ConnectionError("lost")]
    assert do.store_marker(dest, b"x", "m/C1.json").outcome is do.MarkerOutcome.WON


def test_persistent_transport_failure_with_absent_path_is_unknown():
    fake, dest = make()
    fake.fail["files_upload"] = [ConnectionError("down")] * 10
    with pytest.raises(do.MarkerUnknown):
        do.store_marker(dest, b"x", "m/C1.json")


def test_corrupted_marker_upload_is_a_hash_mismatch():
    fake, dest = make()
    fake.corrupt_next_upload = True
    assert do.store_marker(dest, b'{"n":1}', "m/C1.json").outcome is do.MarkerOutcome.HASH_MISMATCH


@pytest.mark.parametrize("same", [False, True])
def test_atomic_server_thread_race_has_exactly_one_winner(same):
    fake, dest = make(atomic=True)
    results, barrier = [], threading.Barrier(16)

    def worker(i):
        barrier.wait()
        results.append(do.store_marker(dest, b"same" if same else f"writer-{i}".encode(), "m/C1.json").outcome)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    c = Counter(results)
    loser = do.MarkerOutcome.EXISTS_SAME if same else do.MarkerOutcome.EXISTS_DIFFERENT
    assert c[do.MarkerOutcome.WON] == 1 and c[loser] == 15 and len(fake.history["/ns/m/c1.json"]) == 1


def test_non_atomic_server_race_is_surfaced_by_the_revision_check():
    """A server without atomic create-if-absent: the rival lands between the loser's check and write; the loser's add overwrites it and must NOT be reported as a win."""
    fake, dest = make(atomic=False)
    fired = []

    def on_upload(path):
        if not fired:
            fired.append(1)
            fake.on_upload = None
            fake.files_upload(b"rival", path, mode=WriteMode.add)

    fake.on_upload = on_upload
    r = do.store_marker(dest, b"loser", "m/C1.json")
    assert r.outcome is do.MarkerOutcome.RACE_SUSPECTED and r.revisions == 2


def test_client_that_cannot_report_revisions_is_not_guessed():
    fake, dest = make()
    fake.files_list_revisions = None
    delattr(type(fake), "files_list_revisions") if False else None

    class NoRev:
        def __init__(self, inner):
            self._i = inner

        def __getattr__(self, n):
            if n == "files_list_revisions":
                raise AttributeError(n)
            return getattr(self._i, n)

    fake2 = FakeDropbox()
    fake2.atomic_add = True
    d2 = DropboxDestination(NoRev(fake2), remote_root="/ns", sleep=lambda s: None)
    r = do.store_marker(d2, b"x", "m/C1.json")
    assert r.outcome is do.MarkerOutcome.WON and r.revisions is None


# ------------------------------------------------------------------------------------------------------------ add-only bytes
def test_store_bytes_add_only_semantics():
    fake, dest = make()
    assert do.store_bytes(dest, b"abc", "o/x").state == "stored"
    assert do.store_bytes(dest, b"abc", "o/x").state == "present_same"
    assert do.store_bytes(dest, b"abd", "o/x").state == "conflict"
    assert fake.files["/ns/o/x"][0] == b"abc"
    fake.folders.add("/ns/o/dir")
    assert do.store_bytes(dest, b"1", "o/dir").state == "conflict"
    fake.corrupt_next_upload = True
    assert do.store_bytes(dest, b"zzzz", "o/y").state == "hash_mismatch"


def test_store_bytes_large_objects_spill_and_stay_add_only(tmp_path):
    fake = SpyFake()
    dest = DropboxDestination(fake, remote_root="/ns", single_shot_limit=8 * 1024 * 1024, sleep=lambda s: None)
    blob = bytes(range(256)) * 40_000                                    # ~10 MB > single-shot
    with pytest.raises(ValueError):
        do.store_bytes(dest, blob, "o/big")
    assert do.store_bytes(dest, blob, "o/big", spill_dir=tmp_path).state == "stored"
    assert do.store_bytes(dest, blob, "o/big", spill_dir=tmp_path).state == "present_same"
    assert do.store_bytes(dest, blob[:-1] + b"\0", "o/big", spill_dir=tmp_path).state == "conflict"
    assert not list(tmp_path.iterdir())


def test_read_verified_bytes(tmp_path):
    fake, dest = make()
    assert do.read_verified_bytes(dest, "o/none", spill_dir=tmp_path) is None
    do.store_bytes(dest, b"hello", "o/x")
    assert do.read_verified_bytes(dest, "o/x", spill_dir=tmp_path) == b"hello"
    with pytest.raises(do.RemoteIntegrityError):
        do.read_verified_bytes(dest, "o/x", spill_dir=tmp_path, max_bytes=2)
    assert not list(tmp_path.iterdir())


# ------------------------------------------------------------------------------------------------------------ range reads
def test_download_range_exact_clamped_and_validated():
    fake, dest = make()
    data = bytes(range(256)) * 10
    do.store_bytes(dest, data, "o/x")
    assert do.download_range(dest, "o/x", 100, 50) == data[100:150]
    assert do.download_range(dest, "o/x", len(data) - 10, 1000) == data[-10:]
    with pytest.raises(ValueError):
        do.download_range(dest, "o/x", len(data), 1)
    with pytest.raises(ValueError):
        do.download_range(dest, "o/x", -1, 5)
    with pytest.raises(do.RangeNotHonoured):
        do.download_range(dest, "o/missing", 0, 5)


def test_download_range_fails_closed_when_the_server_ignores_range_or_lies():
    fake, dest = make()
    do.store_bytes(dest, b"0123456789" * 10, "o/x")
    fake.honour_range = False
    with pytest.raises(do.RangeNotHonoured, match="200"):
        do.download_range(dest, "o/x", 5, 10)
    fake.honour_range = True
    orig = fake.download_range_raw
    fake.download_range_raw = lambda p, s, e: (206, {"Content-Range": "bytes 0-9/100"}, b"x" * 10)
    with pytest.raises(do.RangeNotHonoured, match="Content-Range"):
        do.download_range(dest, "o/x", 5, 10)
    fake.download_range_raw = lambda p, s, e: (206, {"content-range": f"bytes {s}-{e}/100"}, b"x" * 3)
    with pytest.raises(do.RangeNotHonoured, match="received"):
        do.download_range(dest, "o/x", 5, 10)
    fake.download_range_raw = orig


def test_sdk_range_request_builds_a_range_header_and_does_not_leak_the_token(monkeypatch):
    seen = {}

    class R:
        status_code, headers, content = 206, {"Content-Range": "bytes 1-2/9"}, b"ab"

    def post(url, headers, timeout):
        seen.update(url=url, headers=headers)
        return R()

    import requests
    monkeypatch.setattr(requests, "post", post)

    class C:
        _oauth2_access_token = "SECRET-TOKEN"

        def check_and_refresh_access_token(self):
            pass

    status, hdrs, body = do.sdk_range_request(C(), "/ns/o/x", 1, 2)
    assert (status, body) == (206, b"ab") and seen["headers"]["Range"] == "bytes=1-2" and "SECRET-TOKEN" not in repr((status, hdrs, body))
    with pytest.raises(do.RangeNotHonoured):
        do.sdk_range_request(object(), "/ns/o/x", 1, 2)
