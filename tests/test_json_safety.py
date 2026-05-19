"""Unit tests for shane_common.json_safety."""

import dataclasses
import datetime
import math
from enum import Enum

import pytest

from shane_common.json_safety import sanitize_json


class Color(Enum):
    RED = "red"
    BLUE = 42


@dataclasses.dataclass
class Point:
    x: float
    y: float


class TestSanitizeJsonPassthrough:
    def test_none(self):
        assert sanitize_json(None) is None

    def test_true(self):
        assert sanitize_json(True) is True

    def test_false(self):
        assert sanitize_json(False) is False

    def test_int(self):
        assert sanitize_json(42) == 42

    def test_zero(self):
        assert sanitize_json(0) == 0

    def test_string(self):
        assert sanitize_json("hello") == "hello"

    def test_empty_string(self):
        assert sanitize_json("") == ""

    def test_normal_float(self):
        assert sanitize_json(3.14) == pytest.approx(3.14)


class TestSanitizeJsonFloatEdgeCases:
    def test_nan_becomes_none(self):
        assert sanitize_json(float("nan")) is None

    def test_inf_becomes_none(self):
        assert sanitize_json(float("inf")) is None

    def test_neg_inf_becomes_none(self):
        assert sanitize_json(float("-inf")) is None


class TestSanitizeJsonDatetime:
    def test_datetime_naive_includes_utc(self):
        dt = datetime.datetime(2026, 5, 18, 12, 0, 0)
        result = sanitize_json(dt)
        assert isinstance(result, str)
        assert "2026-05-18" in result
        assert "12:00:00" in result

    def test_datetime_aware_preserved(self):
        dt = datetime.datetime(2026, 5, 18, 12, 0, 0, tzinfo=datetime.timezone.utc)
        result = sanitize_json(dt)
        assert isinstance(result, str)
        assert "2026-05-18" in result

    def test_date_isoformat(self):
        d = datetime.date(2026, 5, 18)
        assert sanitize_json(d) == "2026-05-18"

    def test_time_isoformat(self):
        t = datetime.time(14, 30, 0)
        assert sanitize_json(t) == "14:30:00"


class TestSanitizeJsonEnum:
    def test_string_enum_value(self):
        assert sanitize_json(Color.RED) == "red"

    def test_int_enum_value(self):
        assert sanitize_json(Color.BLUE) == 42


class TestSanitizeJsonBytes:
    def test_ascii_bytes(self):
        assert sanitize_json(b"hello") == "hello"

    def test_latin1_bytes_round_trip(self):
        raw = bytes(range(256))
        result = sanitize_json(raw)
        assert isinstance(result, str)
        assert result.encode("latin-1") == raw


class TestSanitizeJsonDataclass:
    def test_dataclass_becomes_dict(self):
        p = Point(x=1.0, y=2.0)
        result = sanitize_json(p)
        assert result == {"x": 1.0, "y": 2.0}

    def test_dataclass_class_itself_falls_through(self):
        # type (the class) is not an instance → falls through to str()
        result = sanitize_json(Point)
        assert isinstance(result, str)


class TestSanitizeJsonCollections:
    def test_list_recursed(self):
        assert sanitize_json([1, "a", None]) == [1, "a", None]

    def test_tuple_becomes_list(self):
        assert sanitize_json((1, 2, 3)) == [1, 2, 3]

    def test_set_becomes_list(self):
        result = sanitize_json({42})
        assert result == [42]

    def test_frozenset_becomes_list(self):
        result = sanitize_json(frozenset({1}))
        assert result == [1]

    def test_dict_string_keys_pass_through(self):
        assert sanitize_json({"a": 1}) == {"a": 1}

    def test_dict_int_keys_become_strings(self):
        result = sanitize_json({1: "one", 2: "two"})
        assert result == {"1": "one", "2": "two"}

    def test_nested_structure(self):
        data = {
            "ts": datetime.datetime(2026, 5, 18, 0, 0, 0),
            "values": [float("nan"), 1, (2, 3)],
            "meta": {"key": Color.RED},
        }
        result = sanitize_json(data)
        assert result["values"][0] is None
        assert result["values"][2] == [2, 3]
        assert result["meta"]["key"] == "red"


class TestSanitizeJsonFallback:
    def test_unknown_type_becomes_string(self):
        class Custom:
            def __str__(self):
                return "custom_repr"

        result = sanitize_json(Custom())
        assert result == "custom_repr"
