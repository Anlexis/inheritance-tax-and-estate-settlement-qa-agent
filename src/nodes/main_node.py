"""AgentCore Platform v1.0"""

# main slot — encapsulates the two central pipeline steps:
#   EstateContextExtract  → classify query_type (constrained label set)
#   InheritanceTaxKBRetrieve → hybrid retrieval over the 4 KB sources
# MainNode orchestrates them sequentially: run each sub-node, merge
# only-changed-fields, short-circuit on ERROR.
#
# Credential hygiene: no credentials in state. The default retrieval is
# in-memory (term-overlap), so it needs no secrets; a real external vector
# store would fetch credentials via the framework secrets API (bound_secrets),
# never from State.
# Audit: audit_log records query_type and kb_sources_used (no PII / no amounts).

import json
import re
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

# Constrained classification labels (never free-form).
_QUERY_TYPES = ("tax_threshold", "deduction", "route_price", "procedure", "deadline")

# Keyword fallback when the LLM is unavailable / returns an off-list label.
_TYPE_KEYWORDS = (
    ("deadline", ("期限", "いつまで", "申告期限", "納付期限")),
    ("route_price", ("路線価", "評価額", "評価")),
    ("tax_threshold", ("基礎控除", "いくらから", "課税", "非課税", "閾値", "しきい値")),
    ("deduction", ("控除", "配偶者", "特例", "軽減")),
    ("procedure", ("手続", "必要書類", "申告方法", "書類", "申請")),
)

# Preferred KB source per query type (light retrieval boost).
_SOURCE_PREFERENCE = {
    "route_price": ("路線価",),
    "deadline": ("国税庁", "法務局"),
    "procedure": ("法務局", "国税庁"),
    "tax_threshold": ("相続税法", "国税庁"),
    "deduction": ("相続税法", "国税庁"),
}

_ARTICLE_RE = re.compile(r"第\s*\d+\s*条(?:の\d+)?")


class EstateContextExtractNode(FunctionNode):
    """Classify the normalized query into one of the constrained query types."""

    # Runs behind the pre_process gate — the external trust boundary is there.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, llm_client: Any | None = None) -> None:
        super().__init__()
        self._llm = llm_client

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A reason settled earlier in the run is the real one: pass it through
        # untouched instead of doing work on input that was already declined.
        marker = state.get("error_code")
        if marker:
            return {"status": AgentStatus.SUCCESS.value, "error_code": marker}
        query = state.get("normalized_query") or state.get("user_input") or ""
        if not query.strip():
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["EstateContextExtract: normalized_query is empty"],
            }

        query_type = self._classify(query)
        audit = list(state.get("audit_log", []))
        audit.append(json.dumps({"step": "EstateContextExtract", "query_type": query_type}, ensure_ascii=False))
        # S-4: the classification label is a closed set (_QUERY_TYPES); the query
        # itself is never emitted.
        emit_trace_event(
            "fin_c2_017.estate_context_extracted",
            {
                "template_id": "FIN-C2-017",
                "query_type": query_type,
                "session_id": state.get("session_id", ""),
            },
            state,
        )
        return {"query_type": query_type, "audit_log": audit, "status": AgentStatus.SUCCESS.value}

    def _classify(self, query: str) -> str:
        # LLM primary (constrained); validate against the label set.
        if self._llm is not None:
            prompt = (
                "次の質問を必ず1語で分類してください。"
                f"許可ラベル: {', '.join(_QUERY_TYPES)}。ラベルのみ出力。\n質問: {query}"
            )
            try:
                label = (self._llm.complete(prompt) or "").strip()
            except Exception:
                label = ""
            if label in _QUERY_TYPES:
                return label
        # Keyword fallback (deterministic).
        return self._keyword_classify(query)

    @staticmethod
    def _keyword_classify(query: str) -> str:
        for qtype, keywords in _TYPE_KEYWORDS:
            if any(k in query for k in keywords):
                return qtype
        return "procedure"


class InheritanceTaxKBRetrieveNode(FunctionNode):
    """Hybrid retrieval over the 相続税法 / 国税庁 / 法務局 / 路線価 KB.

    In-memory term-overlap (keyword substring) + exact-match boost on legal
    article numbers + a light source preference by query_type. The corpus is
    injectable (``corpus``) or lazily loaded from ``kb_path`` JSONL; a missing
    corpus is non-fatal (empty hits → low-confidence handled downstream).
    """

    # Runs behind the pre_process gate — the external trust boundary is there.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, corpus: list[dict[str, Any]] | None = None, kb_path: str | None = None, top_k: int = 5) -> None:
        super().__init__()
        self._corpus = corpus
        self.kb_path = kb_path
        self.top_k = int(top_k)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        query = state.get("normalized_query") or state.get("user_input") or ""
        query_type = state.get("query_type", "procedure")
        articles = set(_norm_article(a) for a in _ARTICLE_RE.findall(query))
        preferred = _SOURCE_PREFERENCE.get(query_type, ())

        scored: list[tuple[float, dict[str, Any]]] = []
        for rec in self._load_corpus():
            score = self._score(rec, query, articles, preferred)
            if score > 0:
                scored.append((score, rec))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        hits = [rec for _, rec in scored[: self.top_k]]

        kb_chunks = [str(r.get("text", "")) for r in hits]
        kb_citations = [str(r.get("citation_id") or r.get("source", "")) for r in hits]

        audit = list(state.get("audit_log", []))
        audit.append(json.dumps({"step": "InheritanceTaxKBRetrieve", "kb_sources_used": len(hits)}, ensure_ascii=False))
        # S-4: counts and the query-type label only — no retrieved KB text and no
        # citation bodies leave the node on this channel.
        emit_trace_event(
            "fin_c2_017.kb_retrieved",
            {
                "template_id": "FIN-C2-017",
                "query_type": query_type,
                "kb_sources_used": len(hits),
                "session_id": state.get("session_id", ""),
            },
            state,
        )
        return {
            "kb_chunks": kb_chunks,
            "kb_citations": kb_citations,
            "audit_log": audit,
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _score(rec: dict[str, Any], query: str, articles: set[str], preferred: tuple[str, ...]) -> float:
        keywords = [k for k in (rec.get("keywords") or []) if isinstance(k, str)]
        kw_hits = sum(1 for k in keywords if k and k in query)
        score = kw_hits / len(keywords) if keywords else 0.0
        # exact-match boost on legal article number (e.g. 相続税法第15条)
        rec_article = _norm_article(str(rec.get("article", "")))
        if rec_article and rec_article in articles:
            score += 2.0
        # source preference boost
        if rec.get("source") in preferred:
            score += 0.25
        return score

    def _load_corpus(self) -> list[dict[str, Any]]:
        if self._corpus is not None:
            return self._corpus
        if not self.kb_path:
            self._corpus = []
            return self._corpus
        records: list[dict[str, Any]] = []
        try:
            with open(self.kb_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(obj, dict):
                        records.append(obj)
        except OSError:
            records = []
        self._corpus = records
        return self._corpus


def _norm_article(a: str) -> str:
    """Normalize a legal article reference for exact matching (strip spaces)."""
    return re.sub(r"\s+", "", a) if a else ""


class MainNode(FunctionNode):
    """Core pipeline node: EstateContextExtract → InheritanceTaxKBRetrieve.

    Sub-nodes (LLM client / KB corpus) are injectable for testability.
    """

    # Runs behind the pre_process gate — the external trust boundary is there.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(
        self,
        llm_client: Any | None = None,
        kb_corpus: list[dict[str, Any]] | None = None,
        kb_path: str | None = None,
        top_k: int = 5,
    ) -> None:
        super().__init__()
        self._extract = EstateContextExtractNode(llm_client=llm_client)
        self._retrieve = InheritanceTaxKBRetrieveNode(corpus=kb_corpus, kb_path=kb_path, top_k=top_k)

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        updates: dict[str, Any] = {}

        result = self._extract.execute({**state, **updates})
        updates.update(result)
        if updates.get("status") == AgentStatus.ERROR.value:
            return updates

        result = self._retrieve.execute({**state, **updates})
        updates.update(result)
        # S-4: MainNode is the workflow boundary. The sub-nodes emit their own
        # domain events; this one records the workflow outcome so a run is
        # traceable even when a sub-node short-circuits.
        emit_trace_event(
            "fin_c2_017.main_completed",
            {
                "template_id": "FIN-C2-017",
                "status": updates.get("status", ""),
                "kb_sources_used": len(updates.get("kb_citations", []) or []),
                "session_id": state.get("session_id", ""),
            },
            state,
        )
        return updates
