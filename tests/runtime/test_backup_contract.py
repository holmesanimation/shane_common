"""Backup-management contract: vocabulary, Purity staleness semantics, cross-process single-flight, durable job history. tmp_path only."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from shane_common.runtime.backup_contract import (
    AuthState, FileSingleFlight, JobKind, JobRecord, JobState, JobStore, RunStatus, SingleFlightBusy, is_stale, last_successful, new_job_id, utc_iso,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)


def rec(jid, state="success", kind="sync_cloud", source="marketdata", done=None, started=None):
    return JobRecord(job_id=jid, kind=kind, source=source, state=state, started_at=started or done, completed_at=done)


def test_vocabulary_matches_purity_names():
    assert {s.value for s in RunStatus} == {"success", "partial", "failed"}
    assert {s.value for s in AuthState} == {"disabled", "auth_required", "ready", "reauth_required"}
    assert {k.value for k in JobKind} == {"sync_archive", "sync_cloud", "preview", "verify", "test_restore"}
    assert JobState.RUNNING.finished is False and JobState.REJECTED.finished and JobState.SUCCESS.finished


def test_staleness_uses_last_successful_run_only():
    old = (NOW - timedelta(days=10)).isoformat()
    recent = (NOW - timedelta(days=1)).isoformat()
    assert is_stale([], 7, now=NOW)
    assert is_stale([rec("a", done=old)], 7, now=NOW) and not is_stale([rec("a", done=recent)], 7, now=NOW)
    assert is_stale([rec("a", done=old), rec("b", state="failed", done=recent)], 7, now=NOW)         # a later failure never resets staleness
    assert not is_stale([rec("a", done=recent), rec("b", state="failed", done=utc_iso())], 7, now=NOW)
    assert is_stale([rec("a", done=recent, source="catalog")], 7, source="marketdata", now=NOW)
    assert is_stale([rec("a", done="garbage")], 7, now=NOW)
    assert last_successful([rec("a", done=old), rec("b", done=recent)]).job_id == "b"


def test_job_record_roundtrip_ignores_unknown_fields():
    r = JobRecord(job_id="j1", kind="verify", source="s", state="running", reasons=["x"], result={"k": 1})
    assert JobRecord.from_dict({**r.to_dict(), "future_field": 1}) == r


def test_store_persists_lists_and_recovers_from_crash(tmp_path):
    s = JobStore(tmp_path / "jobs")
    s.save(rec("1-aaa", done="2026-10-14T00:00:00+00:00", started="2026-10-14T00:00:00+00:00"))
    s.save(JobRecord("2-bbb", "sync_cloud", "marketdata", "running", started_at="2026-10-15T00:00:00+00:00"))
    (tmp_path / "jobs" / "junk.json").write_text("{not json", encoding="utf-8")
    assert [r.job_id for r in s.list_recent()] == ["2-bbb", "1-aaa"] and s.get("1-aaa").state == "success" and s.get("../x") is None and s.get("nope") is None
    assert s.mark_interrupted() == 1
    r = s.get("2-bbb")
    assert r.state == "failed" and "interrupted" in r.message and r.completed_at


def test_store_prunes_to_keep(tmp_path):
    s = JobStore(tmp_path, keep=3)
    for i in range(6):
        s.save(JobRecord(f"{i}-x", "preview", "m", "success", started_at=f"2026-10-1{i}T00:00:00+00:00"))
    s.prune()
    assert [r.job_id for r in s.list_recent()] == ["5-x", "4-x", "3-x"]


def test_single_flight_blocks_second_holder_and_recovers_stale_lock(tmp_path):
    t = [1000.0]
    a = FileSingleFlight(tmp_path / "lock.json", stale_after=60, clock=lambda: t[0])
    b = FileSingleFlight(tmp_path / "lock.json", stale_after=60, clock=lambda: t[0])
    a.acquire("job-a")
    with pytest.raises(SingleFlightBusy) as e:
        b.acquire("job-b")
    assert e.value.holder["job_id"] == "job-a" and "token" not in (b.holder() or {}) and b.holder()["pid"] == os.getpid()
    t[0] += 30
    a.heartbeat()
    t[0] += 45
    with pytest.raises(SingleFlightBusy):
        b.acquire("job-b")                                               # heartbeat kept it alive
    t[0] += 61                                                           # holder died: no heartbeat
    b.acquire("job-b")
    assert b.holder()["job_id"] == "job-b"
    a.release()                                                          # a stale holder cannot release b's lock
    assert b.holder() is not None
    b.release()
    assert b.holder() is None and not (tmp_path / "lock.json").exists()


def test_unreadable_lock_is_recovered(tmp_path):
    (tmp_path / "lock.json").write_text("garbage", encoding="utf-8")
    FileSingleFlight(tmp_path / "lock.json").acquire("j")


def test_no_application_imports():
    import ast
    import shane_common.runtime.backup_contract as m
    names = set()
    for n in ast.walk(ast.parse(open(m.__file__, encoding="utf-8").read())):
        if isinstance(n, ast.Import):
            names |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom) and n.module:
            names.add(n.module.split(".")[0])
    assert not ({"purity_app", "trading_system", "trading_platform", "services", "dropbox"} & names), names
    assert new_job_id().count("-") == 1 and json.dumps(JobRecord("a", "preview", "m", "queued").to_dict())
