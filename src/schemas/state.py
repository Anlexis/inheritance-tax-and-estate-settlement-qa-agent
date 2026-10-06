"""AgentCore Platform v1.0"""

# State must be a flat TypedDict — never Pydantic BaseModel.
# LangGraph checkpoints use msgpack serialization; Pydantic objects
# cause silent corruption.  Extend AgentState with agent-specific
# fields only.  Do NOT add credentials, secrets, or Pydantic models.
# Privacy rule (hard): do NOT store personal financial figures
# (estate values / asset amounts) or other PII in State — public KB
# content, validated case parameters, and classifications only.

import math
from typing import Any, Optional

from framework.schemas.agent_state import AgentState


def _finite_in_range(value: Any, lo: float, hi: float) -> Optional[float]:
    """Parse a caller-controlled numeric: FINITE float within [lo, hi], else None.

    Rejects bools, non-numerics, and — critically — non-finite values: float()
    happily parses "NaN"/"Infinity" (and Python's json accepts bare NaN in
    request bodies), and IEEE NaN comparisons are always False, which turns a
    threshold check into silent FAIL-OPEN. Every caller-supplied number must
    come through here (or an equivalent explicit finite check).
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or not lo <= parsed <= hi:
        return None
    return parsed


class State(AgentState):
    """FIN-C2-017 InheritanceTaxQAAgent state.

    Shared fields (user_input, input_context, status, session_id, node_history,
    error_log, hitl_*, etc.) are inherited from AgentState. Design: docs/02_design.md.

    Pipeline (6 logical nodes → pre_process / main / post_process slots):
    QueryNormalize → EstateContextExtract → InheritanceTaxKBRetrieve →
    DeadlineCalculate → ProcedureChecklistGenerate → ResponseValidate.
    All values are primitives / JSON-serializable (msgpack-safe).
    """

    # ── QueryNormalize ──
    normalized_query: str  # sanitized, normalized question

    # ── Validated caller case parameters (from input_context; never raw) ──
    date_of_death: str  # validated ISO date (YYYY-MM-DD) or "" when not provided
    as_of_date: str  # validated ISO reference date or "" (defaults to today downstream)
    heirs_count: int  # validated statutory-heir count; 0 = not provided

    # ── EstateContextExtract ──
    query_type: str  # tax_threshold | deduction | route_price | procedure | deadline

    # ── InheritanceTaxKBRetrieve ──
    kb_chunks: list[str]  # retrieved policy-KB chunks (public info; no personal figures)
    kb_citations: list[str]  # source IDs for the retrieved chunks

    # ── DeadlineCalculate ──
    deadline_info: dict[str, Any]  # JSON-serializable primitives only (e.g. filing_deadline, days_remaining)

    # ── BasicDeductionCalculate ──
    deduction_info: dict[str, Any]  # statutory basic-deduction figures derived from heirs_count

    # ── ProcedureChecklistGenerate ──
    procedure_draft: str  # drafted procedure checklist / answer body

    # ── ResponseValidate ──
    final_response: str  # validated, citation-grounded final answer (also formatted_output)

    # ── Audit (appended by all nodes) ──
    audit_log: list[str]  # query_type, kb_sources_used, intent — no PII / financial figures
    error_code: Optional[str]
