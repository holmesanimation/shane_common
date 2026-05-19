# shane_common/tests/ui/log_viewer/test_kind_spec.py
"""Tests for KindSpec fields and _DEFAULT_KIND_SPEC fallback."""
from __future__ import annotations

import pytest

from shane_common.ui.log_viewer.kind_spec import (
    KindSpec,
    KindSpecResolver,
    _DEFAULT_KIND_SPEC,
    _disc_eligible,
    _disc_reason,
    _disc_state,
    _format_template,
)


class TestKindSpec:
    def test_required_fields(self):
        spec = KindSpec(severity="INFO", message_template="hello {name}")
        assert spec.severity == "INFO"
        assert spec.message_template == "hello {name}"

    def test_defaults(self):
        spec = KindSpec(severity="DEBUG", message_template="test")
        assert spec.importance == "MEDIUM"
        assert spec.problem is False
        assert spec.dedupe_interval_s is None
        assert spec.dedupe_discriminator is None
        assert spec.severity_fn is None
        assert spec.problem_fn is None
        assert spec.message_fn is None
        assert spec.dedupe_until_changed is False

    def test_frozen(self):
        spec = KindSpec(severity="WARN", message_template="x")
        with pytest.raises((AttributeError, TypeError)):
            spec.severity = "INFO"  # type: ignore[misc]

    def test_default_kind_spec(self):
        assert _DEFAULT_KIND_SPEC.severity == "DEBUG"
        assert _DEFAULT_KIND_SPEC.importance == "LOW"
        assert _DEFAULT_KIND_SPEC.message_template == "{kind}"

    def test_with_all_optional_fields(self):
        spec = KindSpec(
            severity="WARN",
            message_template="Feed {state}",
            importance="HIGH",
            problem=True,
            dedupe_interval_s=60.0,
            dedupe_discriminator=_disc_state,
            severity_fn=lambda p: "INFO" if p.get("ok") else "WARN",
            problem_fn=lambda p: not p.get("ok", True),
            message_fn=lambda p: f"custom: {p.get('x')}",
            dedupe_until_changed=True,
        )
        assert spec.dedupe_interval_s == 60.0
        assert spec.dedupe_discriminator({"state": "healthy"}) == "healthy"
        assert spec.severity_fn({"ok": True}) == "INFO"
        assert spec.problem_fn({"ok": False}) is True
        assert spec.message_fn({"x": "val"}) == "custom: val"
        assert spec.dedupe_until_changed is True


class TestDiscriminatorHelpers:
    def test_disc_eligible(self):
        assert _disc_eligible({"eligible": True}) == "True"
        assert _disc_eligible({}) == ""

    def test_disc_reason(self):
        assert _disc_reason({"reason": "no_plan"}) == "no_plan"
        assert _disc_reason({}) == ""

    def test_disc_state(self):
        assert _disc_state({"state": "healthy"}) == "healthy"
        assert _disc_state({}) == ""


class TestFormatTemplate:
    def test_basic_substitution(self):
        result = _format_template("Hello {name}", {"name": "World"}, {})
        assert result == "Hello World"

    def test_missing_key_becomes_question_mark(self):
        result = _format_template("Value: {missing}", {}, {})
        assert result == "Value: ?"

    def test_envelope_fallback(self):
        result = _format_template("Run {run_id}", {}, {"run_id": "abc123"})
        assert result == "Run abc123"

    def test_payload_overrides_envelope(self):
        result = _format_template(
            "Instrument: {instrument}",
            {"instrument": "TSLA"},
            {"instrument": "AAPL"},
        )
        assert result == "Instrument: TSLA"

    def test_payload_key_excluded(self):
        # "payload" key in envelope should not be included in namespace
        result = _format_template("{kind}", {}, {"kind": "test.event", "payload": {"x": 1}})
        assert result == "test.event"
