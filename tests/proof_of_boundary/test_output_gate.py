# FIN-C2-017 — Proof-of-Boundary: output-gate invariants
#
# The post_process / ResponseValidate boundary is the domain output gate.
# These PB tests assert its non-bypassable invariants:
#   - 税理士 referral is ALWAYS present in final_response (incl. scope-out)
#   - individualized tax-liability requests are scoped out (no calculation)
#   - a credential pattern reaching the drafted answer withholds it (BLOCKER),
#     surviving the full node __call__ (framework gates included)
#   - every monetary token in the response sits on the documented 1,000 grid
#   - each node records an audit entry (no silent failure)

import re

from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.nodes.post_process_node import PostProcessNode, _TAX_ADVISOR_REFERRAL

_GRID_SCAN_RE = re.compile(r"-?\d{1,3}(?:,\d{3})+|-?\d{5,}")


class TestOutputGate:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_advisor_referral_always_present(self):
        """税理士 referral present in final_response across query types."""
        queries = [
            "相続税の必要書類は",
            "基礎控除はいくら",
            "路線価の評価方法",
            "申告期限はいつまで",
            "私の相続税額はいくらになりますか 計算して",  # scope-out path
        ]
        for q in queries:
            out = self.node.execute({"normalized_query": q, "audit_log": []})
            assert _TAX_ADVISOR_REFERRAL in out["final_response"], f"referral missing for: {q}"

    def test_scope_out_blocks_individualized_calculation(self):
        """Individualized tax-liability request → scope-out, no calculation."""
        out = self.node.execute({"normalized_query": "私の税額を計算していくらになるか教えて", "audit_log": []})
        assert "個別の相続税額の計算はこのサービスでは行えません" in out["final_response"]
        assert _TAX_ADVISOR_REFERRAL in out["final_response"]

    def test_credential_in_draft_is_withheld_through_the_full_call(self):
        """A credential-looking token in the drafted answer withholds the answer.

        Runs through node(state) — the full __call__ pipeline — so it also
        proves the sanitised result passes the framework's own output gate
        (nothing credential-shaped remains anywhere in the returned dict).
        """

        class LeakLLM:
            def complete(self, prompt):
                return "手順: sk-ABCDEFGHIJKLMNOPQRSTUVWX0123 を使用"

        node = PostProcessNode(llm_client=LeakLLM())
        out = node(
            {
                "caller_trust_level": TrustLevel.ANONYMOUS.value,
                "normalized_query": "手続き",
                "query_type": "procedure",
                "kb_chunks": ["x"],
                "audit_log": [],
                "execution_time": {},
            }
        )
        assert out["status"] == AgentStatus.SUCCESS.value
        assert "BLOCKER" in out["final_response"]
        assert "sk-" not in out["final_response"]
        assert "sk-" not in out.get("procedure_draft", "")

    def test_every_monetary_token_sits_on_the_grid(self):
        """Approved external schema: every monetary-form figure is a 1,000 multiple."""
        out = self.node.execute(
            {
                "normalized_query": "基礎控除と期限を教えて",
                "query_type": "tax_threshold",
                "heirs_count": 3,
                "date_of_death": "2026-01-15",
                "as_of_date": "2026-08-20",
                "audit_log": [],
            }
        )
        for token in _GRID_SCAN_RE.findall(out["final_response"]):
            value = int(token.replace(",", ""))
            assert value % 1_000 == 0, f"off-grid value leaked: {token}"

    def test_audit_log_appended(self):
        """The node records an audit entry (no silent failure)."""
        out = self.node.execute({"normalized_query": "手続き", "audit_log": []})
        assert isinstance(out["audit_log"], list) and len(out["audit_log"]) >= 1
