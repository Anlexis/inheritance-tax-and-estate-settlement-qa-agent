# FIN-C2-017 — Unit Tests: Main Node

from src.nodes.main_node import MainNode
from framework.schemas.agent_status import AgentStatus


class TestMainNode:
    """Unit tests for the main business logic node."""

    def setup_method(self):
        self.node = MainNode()

    def test_success_path(self):
        """TC: Main node classifies a valid query + retrieves → SUCCESS."""
        state = {
            "normalized_query": "相続税の申告期限はいつまで",
            "audit_log": [],
            "error_log": [],
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.SUCCESS
        assert "query_type" in result
        assert "kb_chunks" in result

    def test_empty_input(self):
        """TC: Main node returns ERROR when the query is empty (input contract)."""
        state = {
            "normalized_query": "",
            "audit_log": [],
            "error_log": [],
        }
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR

    def test_returns_kb_chunks_and_citations(self):
        """EstateContextExtract + InheritanceTaxKBRetrieve write kb_chunks/kb_citations."""
        kb = [
            {
                "source": "国税庁",
                "article": "",
                "title": "申告期限",
                "text": "相続税の申告期限は10か月以内",
                "citation_id": "nta-1",
                "keywords": ["期限", "申告期限"],
            },
        ]
        node = MainNode(kb_corpus=kb, top_k=3)
        result = node.execute({"normalized_query": "申告期限はいつまで", "audit_log": []})
        assert result["status"] == AgentStatus.SUCCESS
        assert isinstance(result["kb_chunks"], list) and result["kb_chunks"]
        assert "nta-1" in result["kb_citations"]

    def test_classifies_route_price(self):
        """EstateContextExtract maps a 路線価 query to query_type 'route_price'."""
        result = self.node.execute({"normalized_query": "路線価の評価方法を教えて", "audit_log": []})
        assert result["query_type"] == "route_price"

    def test_article_exact_match_boost_ranks_cited_article_first(self):
        """A 相続税法第N条 query ranks the exact-article record above keyword hits."""
        kb = [
            {
                "source": "国税庁",
                "article": "",
                "text": "申告の一般案内",
                "citation_id": "nta-guide",
                "keywords": ["申告"],
            },
            {
                "source": "相続税法",
                "article": "第15条",
                "text": "基礎控除は相続税法第15条に定める",
                "citation_id": "law-15",
                "keywords": ["基礎控除"],
            },
        ]
        node = MainNode(kb_corpus=kb, top_k=2)
        result = node.execute({"normalized_query": "相続税法第15条の基礎控除と申告について", "audit_log": []})
        assert result["kb_citations"][0] == "law-15"

    def test_execute_method_signature(self):
        """Node contract: implement execute(state), not _invoke_impl.

        Canonical contract:
          - Override: execute(self, state: AgentState) -> dict
          - Do not override the framework's internal dispatch hooks.
        """
        import inspect

        # Must have execute() defined on the concrete class (not just inherited stub)
        assert hasattr(MainNode, "execute"), "MainNode must implement execute()"

        sig = inspect.signature(MainNode.execute)
        params = list(sig.parameters.keys())
        # execute(self, state) — at minimum two parameters
        assert len(params) >= 2, f"execute() must accept (self, state), got params: {params}"
        assert params[1] == "state", f"Second parameter must be 'state', got '{params[1]}'"

        # Must NOT define _invoke_impl at the domain level
        assert (
            "_invoke_impl" not in MainNode.__dict__
        ), "_invoke_impl() must not be defined in MainNode — use execute() instead"
