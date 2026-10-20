"""Cooperative cancellation at safe boundaries (M5-4A): upload sessions stay resumable, nothing is claimed verified. In-memory Dropbox double; tmp_path only."""

from __future__ import annotations

import os

from shane_common.runtime.backup_contract import JobRecord, JobState, JobStore
from shane_common.runtime.transfer import dropbox_destination as dd
from shane_common.runtime.transfer.dropbox_destination import DropboxDestination, UploadOutcome as O
from shane_common.runtime.transfer.dropbox_testing import FakeDropbox

MIB = 1024 * 1024
KEY = "/bk/big/big.bin"


def dest(fake, prog=None):
    return DropboxDestination(fake, remote_root="/bk", single_shot_limit=4 * MIB, chunk_size=4 * MIB, progress=prog or dd.InMemoryProgress(), sleep=lambda s: None)


def test_cancel_between_session_chunks_keeps_progress_and_resumes(tmp_path):
    fake, prog = FakeDropbox(), dd.InMemoryProgress()
    p = tmp_path / "big.bin"
    p.write_bytes(os.urandom(20 * MIB))
    calls = {"n": 0}

    def cancel_after_two_chunks():
        calls["n"] += 1
        return calls["n"] > 3                                                          # 1 pre-upload check, then after chunk 1 and 2
    r = dest(fake, prog).store_file(p, "big/big.bin", should_cancel=cancel_after_two_chunks)
    assert r.outcome == O.CANCELLED and not r.verified and r.bytes_sent == 0
    saved = prog.get(KEY.lower()) or prog.get(KEY)
    assert saved is not None and saved["offset"] >= 4 * MIB                              # progress persisted at the chunk boundary
    assert KEY.lower() not in fake.files                                                 # nothing was committed remotely
    fake.calls.clear()
    r2 = dest(fake, prog).store_file(p, "big/big.bin")                                   # a later run resumes from the saved offset
    assert r2.outcome == O.VERIFIED_UPLOADED and r2.resumed and "files_upload_session_start" not in fake.calls
    assert fake.files[KEY.lower()][0] == p.read_bytes()


def test_cancel_before_any_byte_is_sent(tmp_path):
    fake = FakeDropbox()
    p = tmp_path / "f.bin"
    p.write_bytes(b"x")
    r = dest(fake).store_file(p, "a/f.bin", should_cancel=lambda: True)
    assert r.outcome == O.CANCELLED and not fake.files and not any(c.startswith("files_upload") for c in fake.calls)


def test_a_file_that_finishes_before_the_next_boundary_is_still_verified(tmp_path):
    fake = FakeDropbox()
    p = tmp_path / "f.bin"
    p.write_bytes(b"small")
    flag = {"v": False}

    def late():
        v = flag["v"]
        flag["v"] = True                                                                   # cancel requested only after the pre-upload check
        return v
    r = dest(fake).store_file(p, "a/f.bin", should_cancel=late)
    assert r.outcome == O.VERIFIED_UPLOADED and r.verified                                 # single-shot upload has no inner boundary: it completes and verifies


def test_cancel_flag_file_roundtrip_and_state_vocabulary(tmp_path):
    s = JobStore(tmp_path)
    s.save(JobRecord("1-a", "sync_cloud", "m", "running", started_at="2026-10-15T00:00:00+00:00"))
    assert not s.cancel_requested("1-a") and s.get("1-a").cancel_requested is False
    s.request_cancel("1-a")
    assert s.cancel_requested("1-a") and s.get("1-a").cancel_requested is True and s.list_recent()[0].cancel_requested is True
    r = s.get("1-a")
    r.state = JobState.CANCELLED.value
    s.save(r)
    assert s.get("1-a").cancel_requested is False                                          # a finished job never shows a pending cancel
    s.clear_cancel("1-a")
    assert not s.cancel_requested("1-a") and JobState.CANCELLED.finished
    for bad in ("../x", "", "a/b"):
        assert s.cancel_requested(bad) is False
    import pytest
    with pytest.raises(ValueError):
        s.request_cancel("../evil")
