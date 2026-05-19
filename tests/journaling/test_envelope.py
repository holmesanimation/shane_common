"""Tests for shane_common.journaling.envelope."""

import datetime
import pytest

from shane_common.journaling.envelope import JournalSource, build_journal_envelope
from shane_common.journaling.profile import JournalProfile, DefaultJournalProfile
from pathlib import Path
from typing import Optional


class _StrictProfile(JournalProfile):
    """Only allows 'test.event'; rejects everything else."""

    def validate_kind(self, kind: str) -> Optional[str]:
        if kind != "test.event":
            return f"Invalid kind: {kind!r}"
        return None

    def stream_for_kind(self, kind: str) -> str:
        return "test"

    def route_event(self, envelope: dict) -> Path:
        raise NotImplementedError


_SOURCE = JournalSource(app="testapp", component="tester")


class TestBuildJournalEnvelope:
    def test_local_ts_is_first_key(self):
        env = build_journal_envelope(
            kind="test.event",
            run_id="run-1",
            source=_SOURCE,
            payload={"x": 1},
        )
        assert list(env.keys())[0] == "local_TS"

    def test_ts_is_present_and_utc_parseable(self):
        env = build_journal_envelope(
            kind="test.event",
            run_id="run-1",
            source=_SOURCE,
            payload={},
        )
        assert "ts" in env
        dt = datetime.datetime.fromisoformat(env["ts"])
        assert dt.tzinfo is not None

    def test_kind_present(self):
        env = build_journal_envelope(
            kind="test.event",
            run_id="run-1",
            source=_SOURCE,
            payload={},
        )
        assert env["kind"] == "test.event"

    def test_run_id_present(self):
        env = build_journal_envelope(
            kind="test.event",
            run_id="run-1",
            source=_SOURCE,
            payload={"y": 2},
        )
        assert env["run_id"] == "run-1"

    def test_payload_present(self):
        env = build_journal_envelope(
            kind="test.event",
            run_id="run-1",
            source=_SOURCE,
            payload={"z": 3},
        )
        assert env["payload"] == {"z": 3}

    def test_invalid_kind_with_strict_profile_raises(self):
        profile = _StrictProfile()
        with pytest.raises(ValueError):
            build_journal_envelope(
                kind="bad.kind",
                run_id="run-1",
                source=_SOURCE,
                payload={},
                profile=profile,
            )

    def test_valid_kind_with_strict_profile_passes(self):
        profile = _StrictProfile()
        env = build_journal_envelope(
            kind="test.event",
            run_id="run-1",
            source=_SOURCE,
            payload={},
            profile=profile,
        )
        assert env["kind"] == "test.event"

    def test_sanitization_strips_nan(self):
        env = build_journal_envelope(
            kind="test.event",
            run_id="run-1",
            source=_SOURCE,
            payload={"x": float("nan")},
        )
        assert env["payload"]["x"] is None

    def test_msg_omitted_when_none(self):
        env = build_journal_envelope(
            kind="test.event",
            run_id="run-1",
            source=_SOURCE,
            payload={},
        )
        assert "msg" not in env

    def test_msg_present_when_provided(self):
        env = build_journal_envelope(
            kind="test.event",
            run_id="run-1",
            source=_SOURCE,
            payload={},
            msg="hello",
        )
        assert env["msg"] == "hello"

    def test_app_clock_ts_used_for_ts(self):
        # 1704067200 == 2024-01-01 00:00:00 UTC
        env = build_journal_envelope(
            kind="test.event",
            run_id="run-1",
            source=_SOURCE,
            payload={},
            app_clock_ts=1_704_067_200.0,
        )
        dt = datetime.datetime.fromisoformat(env["ts"])
        assert dt.astimezone(datetime.timezone.utc).date().isoformat() == "2024-01-01"
