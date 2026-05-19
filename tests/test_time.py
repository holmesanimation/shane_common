"""Unit tests for shane_common.time."""

import datetime
import pytest

from shane_common.time import (
    utc_now_iso,
    normalize_epoch_seconds,
    day_bucket_from_ts,
    local_now_iso,
    local_iso_from_epoch,
    utc_iso_from_epoch,
)


class TestUtcNowIso:
    def test_returns_string(self):
        result = utc_now_iso()
        assert isinstance(result, str)

    def test_contains_timezone_offset(self):
        result = utc_now_iso()
        # fromisoformat should not raise and should have tzinfo
        dt = datetime.datetime.fromisoformat(result)
        assert dt.tzinfo is not None

    def test_parseable_as_utc(self):
        result = utc_now_iso()
        dt = datetime.datetime.fromisoformat(result)
        assert dt.utcoffset() == datetime.timedelta(0)


class TestNormalizeEpochSeconds:
    def test_small_int_unchanged(self):
        assert normalize_epoch_seconds(1_000_000_000) == pytest.approx(1_000_000_000.0)

    def test_milliseconds_divided_by_1000(self):
        ms = 1_716_000_000_000
        result = normalize_epoch_seconds(ms)
        assert result == pytest.approx(ms / 1000.0)

    def test_float_input(self):
        result = normalize_epoch_seconds(1_716_000_000.5)
        assert result == pytest.approx(1_716_000_000.5)

    def test_boundary_below_threshold_unchanged(self):
        # 1e10 - 1 should be treated as seconds
        val = 9_999_999_999
        assert normalize_epoch_seconds(val) == pytest.approx(float(val))

    def test_boundary_above_threshold_divided(self):
        # 1e10 + 1 should be treated as milliseconds
        val = 10_000_000_001
        assert normalize_epoch_seconds(val) == pytest.approx(val / 1000.0)


class TestDayBucketFromTs:
    def test_epoch_seconds_int(self):
        # 2024-01-01 00:00:00 UTC
        ts = 1_704_067_200
        assert day_bucket_from_ts(ts) == "2024-01-01"

    def test_epoch_milliseconds_auto_detected(self):
        ts_ms = 1_704_067_200_000
        assert day_bucket_from_ts(ts_ms) == "2024-01-01"

    def test_epoch_float(self):
        ts = 1_704_067_200.5
        assert day_bucket_from_ts(ts) == "2024-01-01"

    def test_iso_string_with_tz(self):
        assert day_bucket_from_ts("2026-05-18T12:00:00+00:00") == "2026-05-18"

    def test_iso_string_naive_treated_as_utc(self):
        assert day_bucket_from_ts("2026-05-18T12:00:00") == "2026-05-18"

    def test_datetime_with_tz(self):
        dt = datetime.datetime(2026, 5, 18, 12, 0, 0, tzinfo=datetime.timezone.utc)
        assert day_bucket_from_ts(dt) == "2026-05-18"

    def test_datetime_naive_treated_as_utc(self):
        dt = datetime.datetime(2026, 5, 18, 12, 0, 0)
        assert day_bucket_from_ts(dt) == "2026-05-18"

    def test_date_object(self):
        d = datetime.date(2026, 5, 18)
        assert day_bucket_from_ts(d) == "2026-05-18"

    def test_cross_midnight_boundary(self):
        # 2024-01-01 23:59:59 UTC should still be 2024-01-01
        ts = 1_704_153_599
        assert day_bucket_from_ts(ts) == "2024-01-01"


class TestLocalNowIso:
    def test_returns_string_with_tzinfo(self):
        result = local_now_iso()
        dt = datetime.datetime.fromisoformat(result)
        assert dt.tzinfo is not None

    def test_parseable_as_iso8601(self):
        result = local_now_iso()
        dt = datetime.datetime.fromisoformat(result)
        assert isinstance(dt, datetime.datetime)


class TestLocalIsoFromEpoch:
    def test_parseable_with_tzinfo(self):
        result = local_iso_from_epoch(1_704_067_200)
        dt = datetime.datetime.fromisoformat(result)
        assert dt.tzinfo is not None

    def test_returns_string(self):
        result = local_iso_from_epoch(1_704_067_200)
        assert isinstance(result, str)


class TestUtcIsoFromEpoch:
    def test_utc_date_for_known_epoch(self):
        # 1704067200 == 2024-01-01 00:00:00 UTC
        result = utc_iso_from_epoch(1_704_067_200)
        dt = datetime.datetime.fromisoformat(result)
        assert dt.astimezone(datetime.timezone.utc).date().isoformat() == "2024-01-01"

    def test_parseable_as_utc_iso8601(self):
        result = utc_iso_from_epoch(1_704_067_200)
        dt = datetime.datetime.fromisoformat(result)
        assert dt.tzinfo is not None
