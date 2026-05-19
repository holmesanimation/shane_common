"""Tests for AppSession factory and new_run_id."""

import os
import re
from datetime import datetime

import pytest

from shane_common.sessions.app_session import AppSession, make_app_session, new_run_id

UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


class TestNewRunId:
    def test_is_uuid4(self):
        assert UUID4_RE.match(new_run_id())

    def test_unique(self):
        assert new_run_id() != new_run_id()


class TestMakeAppSession:
    def test_run_id_is_uuid4(self, tmp_path):
        session = make_app_session("myapp", "MyApp", str(tmp_path), str(tmp_path / "_system"))
        assert UUID4_RE.match(session.run_id)

    def test_app_id_matches(self, tmp_path):
        session = make_app_session("myapp", "MyApp", str(tmp_path), str(tmp_path / "_system"))
        assert session.app_id == "myapp"

    def test_app_name_matches(self, tmp_path):
        session = make_app_session("myapp", "MyApp", str(tmp_path), str(tmp_path / "_system"))
        assert session.app_name == "MyApp"

    def test_started_utc_iso_parseable_with_tzinfo(self, tmp_path):
        session = make_app_session("myapp", "MyApp", str(tmp_path), str(tmp_path / "_system"))
        dt = datetime.fromisoformat(session.started_utc_iso)
        assert dt.tzinfo is not None

    def test_started_local_iso_parseable_with_tzinfo(self, tmp_path):
        session = make_app_session("myapp", "MyApp", str(tmp_path), str(tmp_path / "_system"))
        dt = datetime.fromisoformat(session.started_local_iso)
        assert dt.tzinfo is not None

    def test_pid_matches_current_process(self, tmp_path):
        session = make_app_session("myapp", "MyApp", str(tmp_path), str(tmp_path / "_system"))
        assert session.pid == os.getpid()
