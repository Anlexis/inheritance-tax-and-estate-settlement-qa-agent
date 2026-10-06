# FIN-C2-017 — Unit Tests: PreProcessNode (QueryNormalize + caller-data validation)
#
# The caller-data contract is hostile-input-first: every supported
# input_context field is validated against explicit bounds and fails CLOSED
# (status error naming the FIELD, never echoing the value). Non-finite numerics
# ("NaN"/"Infinity" strings AND raw float nan/inf — both parse via float(), and
# raw JSON can deliver bare NaN) must never pass, because NaN comparisons are
# always False and would silently disable the downstream logic.

import json

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.pre_process_node import PreProcessNode


class TestQueryNormalize:
    def setup_method(self):
        self.node = PreProcessNode()

    def test_redacts_monetary_value(self):
        """A monetary/estate value in input is redacted, never stored."""
        out = self.node.execute({"user_input": "遺産が3億円ある場合の基礎控除は？"})
        assert out["status"] == AgentStatus.SUCCESS
        assert "3億円" not in out["normalized_query"]
        assert "[金額]" in out["normalized_query"]
        # and no amount leaks into the audit log
        assert all("3億" not in entry for entry in out["audit_log"])

    def test_detects_intent_hint(self):
        """Intent hint is logged to audit_log (deadline keyword → 'deadline')."""
        out = self.node.execute({"user_input": "相続税の申告期限はいつまで"})
        assert json.loads(out["audit_log"][-1])["intent_detected"] == "deadline"

    def test_empty_input_returns_error(self):
        """Empty/missing input → error status with error_log."""
        out = self.node.execute({"user_input": "   "})
        assert out["status"] == AgentStatus.SUCCESS
        assert out["error_log"]
        assert "normalized_query" not in out

    def test_html_stripped(self):
        """Markup is stripped from the normalized query."""
        out = self.node.execute({"user_input": "<b>必要書類</b>は？"})
        assert "<b>" not in out["normalized_query"]


class TestCaseContextValidation:
    """input_context is untrusted: strict per-field validation, fail CLOSED."""

    def setup_method(self):
        self.node = PreProcessNode()

    def _run(self, input_context):
        return self.node.execute({"user_input": "申告期限は？", "input_context": input_context})

    def test_absent_context_degrades_to_baseline(self):
        """No caller data → success with baseline (empty) case parameters."""
        out = self._run({})
        assert out["status"] == AgentStatus.SUCCESS
        assert out["date_of_death"] == ""
        assert out["as_of_date"] == ""
        assert out["heirs_count"] == 0

    def test_valid_fields_pass_through_canonically(self):
        out = self._run({"date_of_death": "2026-01-15", "as_of_date": "2026-08-01", "heirs_count": 3})
        assert out["status"] == AgentStatus.SUCCESS
        assert out["date_of_death"] == "2026-01-15"
        assert out["as_of_date"] == "2026-08-01"
        assert out["heirs_count"] == 3

    def test_unknown_context_keys_are_ignored(self):
        """Fields outside the contract (e.g. platform conversation keys) are ignored."""
        out = self._run({"conversation_history": ["irrelevant"], "heirs_count": 2})
        assert out["status"] == AgentStatus.SUCCESS
        assert out["heirs_count"] == 2
        assert "conversation_history" not in out

    def test_non_dict_input_context_is_rejected(self):
        out = self.node.execute({"user_input": "申告期限は？", "input_context": "not-a-dict"})
        assert out["status"] == AgentStatus.SUCCESS

    @pytest.mark.parametrize(
        "bad_heirs",
        [
            "NaN",
            "Infinity",
            "-Infinity",
            float("nan"),
            float("inf"),
            float("-inf"),
            0,
            -1,
            51,
            2.5,
            "3.5",
            True,
            [],
            {},
            "three",
        ],
        ids=[
            "str-nan",
            "str-inf",
            "str-neginf",
            "raw-nan",
            "raw-inf",
            "raw-neginf",
            "zero",
            "negative",
            "over-max",
            "float-frac",
            "str-frac",
            "bool",
            "list",
            "dict",
            "word",
        ],
    )
    def test_heirs_count_matrix_fails_closed(self, bad_heirs):
        """Every non-finite / out-of-range / non-integer heirs_count is rejected."""
        out = self._run({"heirs_count": bad_heirs})
        assert out["status"] == AgentStatus.SUCCESS
        assert any("heirs_count" in e for e in out["error_log"])
        assert "normalized_query" not in out

    @pytest.mark.parametrize("field", ["date_of_death", "as_of_date"])
    @pytest.mark.parametrize(
        "bad_date",
        [
            "2026/01/15",
            "15-01-2026",
            "2026-13-01",
            "2026-02-30",
            "1899-12-31",
            "2101-01-01",
            "not-a-date",
            20260115,
            "",
            "2026-1-5",
            "2026-01-15T00:00:00",
        ],
        ids=[
            "slashes",
            "dmy",
            "bad-month",
            "impossible-day",
            "below-min-year",
            "above-max-year",
            "word",
            "int",
            "empty",
            "unpadded",
            "datetime",
        ],
    )
    def test_date_matrix_fails_closed(self, field, bad_date):
        """Every malformed / out-of-bounds date is rejected, naming the field."""
        out = self._run({field: bad_date})
        assert out["status"] == AgentStatus.SUCCESS
        assert any(field in e for e in out["error_log"])

    def test_rejected_values_are_never_echoed(self):
        """Error messages name the field — the offending value must not round-trip."""
        marker = "SENSITIVE-VALUE-987654321"
        out = self._run({"date_of_death": marker})
        assert out["status"] == AgentStatus.SUCCESS
        assert all(marker not in e for e in out["error_log"])

    def test_none_valued_fields_are_treated_as_absent(self):
        out = self._run({"date_of_death": None, "heirs_count": None})
        assert out["status"] == AgentStatus.SUCCESS
        assert out["date_of_death"] == ""
        assert out["heirs_count"] == 0

    def test_audit_records_flags_not_values(self):
        """Audit log carries presence booleans, never the dates themselves."""
        out = self._run({"date_of_death": "2026-01-15", "heirs_count": 2})
        entry = json.loads(out["audit_log"][-1])
        assert entry["has_date_of_death"] is True
        assert entry["has_heirs_count"] is True
        assert all("2026-01-15" not in e for e in out["audit_log"])
