# FIN-C2-017 — Unit Tests: PostProcessNode (Deadline / Deduction / Checklist / ResponseValidate)

import pytest

from framework.schemas.agent_status import AgentStatus

from src.nodes.post_process_node import PostProcessNode, _TAX_ADVISOR_REFERRAL


class TestDeadlineCalculate:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_deadline_calculate_10_months(self):
        """DeadlineCalculate: filing deadline = date_of_death + 10 months (相続税法第27条)."""
        out = self.node.execute(
            {"normalized_query": "申告期限", "date_of_death": "2026-01-10", "as_of_date": "2026-06-04", "audit_log": []}
        )
        assert out["status"] == AgentStatus.SUCCESS
        assert out["deadline_info"]["available"] is True
        assert out["deadline_info"]["filing_deadline"] == "2026-11-10"
        assert out["deadline_info"]["days_remaining"] == 159
        assert out["deadline_info"]["within_30_days"] is False
        assert out["deadline_info"]["deadline_passed"] is False

    def test_deadline_unavailable_without_date(self):
        """No date_of_death → deadline marked unavailable (graceful baseline)."""
        out = self.node.execute({"normalized_query": "基礎控除は", "audit_log": []})
        assert out["deadline_info"]["available"] is False

    @pytest.mark.parametrize(
        ("dod", "expected"),
        [
            ("2025-04-30", "2026-02-28"),  # Feb has no day 30 → clamped (non-leap)
            ("2027-04-30", "2028-02-29"),  # leap-year February clamps to 29
            ("2026-03-31", "2027-01-31"),  # +10 months across the year boundary
            ("2026-07-31", "2027-05-31"),  # 31-day → 31-day month
        ],
    )
    def test_month_arithmetic_clamps_to_valid_days(self, dod, expected):
        out = self.node.execute({"normalized_query": "期限", "date_of_death": dod, "audit_log": []})
        assert out["deadline_info"]["filing_deadline"] == expected

    def test_within_30_days_warning_rendered(self):
        out = self.node.execute(
            {"normalized_query": "申告期限", "date_of_death": "2025-11-01", "as_of_date": "2026-08-20", "audit_log": []}
        )
        # deadline 2026-09-01, as-of 2026-08-20 → 12 days remaining
        assert out["deadline_info"]["within_30_days"] is True
        assert "【期限警告】" in out["final_response"]
        assert "残り12日" in out["final_response"]

    def test_deadline_passed_notice_rendered(self):
        out = self.node.execute(
            {"normalized_query": "申告期限", "date_of_death": "2025-01-01", "as_of_date": "2026-08-20", "audit_log": []}
        )
        assert out["deadline_info"]["deadline_passed"] is True
        assert "【期限注意】" in out["final_response"]


class TestBasicDeduction:
    def setup_method(self):
        self.node = PostProcessNode()

    @pytest.mark.parametrize(
        ("heirs", "expected_yen"),
        [(1, 36_000_000), (2, 42_000_000), (3, 48_000_000), (10, 90_000_000)],
    )
    def test_statutory_formula(self, heirs, expected_yen):
        """基礎控除 = 30,000,000 + 6,000,000 × heirs (相続税法第15条)."""
        out = self.node.execute({"normalized_query": "基礎控除は", "heirs_count": heirs, "audit_log": []})
        assert out["deduction_info"]["available"] is True
        assert out["deduction_info"]["total_yen"] == expected_yen
        assert f"{expected_yen:,d}円" in out["final_response"]

    def test_unavailable_without_heirs(self):
        out = self.node.execute({"normalized_query": "基礎控除は", "audit_log": []})
        assert out["deduction_info"]["available"] is False
        assert "基礎控除額の目安" not in out["final_response"]

    def test_rendered_amount_sits_on_the_external_grid(self):
        """The rendered deduction is a comma-grouped multiple of 1,000 (schema)."""
        out = self.node.execute({"normalized_query": "基礎控除は", "heirs_count": 7, "audit_log": []})
        assert out["deduction_info"]["total_yen"] % 1_000 == 0
        assert "72,000,000円" in out["final_response"]

    def test_statutory_prose_is_not_corrupted_by_the_gate(self):
        """The formula prose (3,000万円 / 600万円) must render byte-identical."""
        out = self.node.execute({"normalized_query": "基礎控除は", "heirs_count": 2, "audit_log": []})
        assert "3,000万円 + 600万円" in out["final_response"]

    def test_defensive_reject_of_unvalidated_heirs(self):
        """A non-int smuggled into state (bypassing pre_process) is not used."""
        out = self.node.execute({"normalized_query": "基礎控除は", "heirs_count": float("nan"), "audit_log": []})
        assert out["deduction_info"]["available"] is False


class TestResponseValidate:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_referral_present(self):
        """税理士 referral is injected into final_response."""
        out = self.node.execute({"normalized_query": "必要書類は", "query_type": "procedure", "audit_log": []})
        assert _TAX_ADVISOR_REFERRAL in out["final_response"]

    def test_scope_out_no_calculation(self):
        """Individualized tax-liability request is scoped out (no calculation)."""
        out = self.node.execute({"normalized_query": "私の税額を計算していくらか教えて", "audit_log": []})
        assert "個別の相続税額の計算はこのサービスでは行えません" in out["final_response"]
        assert _TAX_ADVISOR_REFERRAL in out["final_response"]

    def test_scope_out_suppresses_deduction_figures(self):
        """The scope-out path renders no computed figures even when data exists."""
        out = self.node.execute(
            {"normalized_query": "私の税額を計算して", "heirs_count": 3, "date_of_death": "2026-01-01", "audit_log": []}
        )
        assert "基礎控除額の目安" not in out["final_response"]
        assert "48,000,000" not in out["final_response"]

    def test_citations_from_retrieved_chunks(self):
        # Citation IDs use a gate-inert form (lowercase source prefix): a
        # standalone 3-letter UPPERCASE prefix ("NTA-15") would read as a
        # currency code + signed value to the output gate, which fails SAFE
        # by snapping it — so corpus citation_ids are lowercase by convention.
        out = self.node.execute(
            {
                "normalized_query": "基礎控除の根拠は",
                "query_type": "tax_threshold",
                "kb_chunks": ["基礎控除は相続税法第15条に定める"],
                "kb_citations": ["nta-15"],
                "audit_log": [],
            }
        )
        assert "根拠: 相続税法第15条、nta-15" in out["final_response"]

    def test_formatted_output_matches_final_response(self):
        """The framework surfaces state['formatted_output'] — both fields carry the gated answer."""
        out = self.node.execute({"normalized_query": "必要書類は", "audit_log": []})
        assert out["formatted_output"] == out["final_response"]
