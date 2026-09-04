"""Tests for generic file-transfer safety primitives (WP8D shane_common layer)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from shane_common.runtime.transfer.copy import (
    CopyOutcome,
    copy_file_verified,
)
from shane_common.runtime.transfer.hashing import sha256_file
from shane_common.runtime.transfer.manifest_store import DurableRecordLedger


# ---------------------------------------------------------------------------
# hashing
# ---------------------------------------------------------------------------

def test_sha256_file_matches_known_digest(tmp_path):
    import hashlib

    p = tmp_path / "a.txt"
    p.write_bytes(b"hello world")
    assert sha256_file(p) == hashlib.sha256(b"hello world").hexdigest()


def test_sha256_file_chunking_produces_same_digest(tmp_path):
    p = tmp_path / "big.bin"
    p.write_bytes(b"x" * 5000)
    whole = sha256_file(p, chunk_size=1024 * 1024)
    chunked = sha256_file(p, chunk_size=7)
    assert whole == chunked


# ---------------------------------------------------------------------------
# copy_file_verified
# ---------------------------------------------------------------------------

def test_copy_fresh_destination(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload")
    dst = tmp_path / "dst" / "src.bin"

    result = copy_file_verified(src, dst)

    assert result.outcome is CopyOutcome.COPIED
    assert dst.read_bytes() == b"payload"
    assert result.destination_hash == result.source_hash
    assert result.destination_size == result.source_size


def test_copy_idempotent_rerun_same_content(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload")
    dst = tmp_path / "dst.bin"

    first = copy_file_verified(src, dst)
    second = copy_file_verified(src, dst)

    assert first.outcome is CopyOutcome.COPIED
    assert second.outcome is CopyOutcome.ALREADY_PRESENT_IDENTICAL
    assert dst.read_bytes() == b"payload"


def test_copy_destination_conflict_does_not_overwrite(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"new content")
    dst = tmp_path / "dst.bin"
    dst.write_bytes(b"different existing content")

    result = copy_file_verified(src, dst)

    assert result.outcome is CopyOutcome.DESTINATION_CONFLICT
    assert dst.read_bytes() == b"different existing content"  # untouched


def test_copy_no_partial_destination_file_left_on_disk(tmp_path):
    """A copy never leaves a stray temp file behind in the destination dir."""
    src = tmp_path / "src.bin"
    src.write_bytes(b"payload" * 1000)
    dst_dir = tmp_path / "dst"
    dst = dst_dir / "src.bin"

    copy_file_verified(src, dst)

    leftovers = [p for p in dst_dir.iterdir() if p.name != "src.bin"]
    assert leftovers == []


def test_copy_source_changed_during_copy_detected(tmp_path, monkeypatch):
    """Simulate the source mutating between the pre-copy hash and the
    post-copy re-hash by monkeypatching shutil.copyfile to mutate source."""
    import shane_common.runtime.transfer.copy as copy_mod

    src = tmp_path / "src.bin"
    src.write_bytes(b"original")
    dst = tmp_path / "dst.bin"

    real_copyfile = copy_mod.shutil.copyfile

    def _mutating_copyfile(s, d):
        real_copyfile(s, d)
        Path(s).write_bytes(b"mutated-after-copy-started")

    monkeypatch.setattr(copy_mod.shutil, "copyfile", _mutating_copyfile)

    result = copy_file_verified(src, dst)

    assert result.outcome is CopyOutcome.SOURCE_CHANGED_DURING_COPY
    assert not dst.exists()


# ---------------------------------------------------------------------------
# DurableRecordLedger
# ---------------------------------------------------------------------------

def test_ledger_upsert_and_get(tmp_path):
    ledger = DurableRecordLedger(tmp_path / "manifest.json")
    ledger.upsert("mig-1", {"state": "discovered", "n": 1})
    assert ledger.get("mig-1") == {"state": "discovered", "n": 1}
    assert ledger.get("missing") is None


def test_ledger_reload_survives_process_restart(tmp_path):
    path = tmp_path / "manifest.json"
    ledger1 = DurableRecordLedger(path)
    ledger1.upsert("mig-1", {"state": "verified"})

    ledger2 = DurableRecordLedger(path)  # simulates a fresh process
    assert ledger2.get("mig-1") == {"state": "verified"}


def test_ledger_delete_removes_record(tmp_path):
    ledger = DurableRecordLedger(tmp_path / "manifest.json")
    ledger.upsert("mig-1", {"state": "discovered"})
    ledger.delete("mig-1")
    assert ledger.get("mig-1") is None


def test_ledger_empty_file_loads_as_empty_dict(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("", encoding="utf-8")
    ledger = DurableRecordLedger(path)
    assert ledger.load() == {}
