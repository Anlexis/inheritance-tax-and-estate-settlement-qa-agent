"""AgentCore Platform v1.0"""

# QueryNormalize (pre_process slot).
# Node contract:
#  - Extend FunctionNode; implement execute(state) -> dict
#  - Return ONLY the fields this node changes (never full state)
#
# Privacy (hard): NEVER write estate values / personal monetary amounts to
# state or audit_log. Monetary figures are redacted from the stored query;
# only the detected intent type is logged.
# Input hygiene: the question is sanitized (HTML stripped, trimmed), and every
# caller-supplied case parameter in input_context is validated against explicit
# bounds before it can influence the pipeline (fail CLOSED; the offending FIELD
# is named in errors, never the offending value).

import json
import re
from datetime import date
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.progress import emit_progress
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED

from src.schemas.state import _finite_in_range

_HTML = re.compile(r"<[^>]+>")

# Monetary redaction: numbers adjacent to a currency/scale marker (億/万/千/円)
# only — does not touch bare counts (e.g. "3人"). Half- and full-width digits.
_MONEY = re.compile(r"[0-9０-９][0-9０-９,，]*\s*(?:億|万|千)+\s*円?|[0-9０-９][0-9０-９,，]*\s*円")
_REDACTION = "[金額]"

# Intent hint only (the authoritative query_type is set by EstateContextExtract).
_INTENT_KEYWORDS = (
    ("deadline", ("期限", "いつまで", "申告期限", "納付期限")),
    ("tax_threshold", ("基礎控除", "いくらから", "課税", "非課税", "閾値", "しきい値")),
    ("deduction", ("控除", "配偶者", "特例", "軽減")),
    ("route_price", ("路線価", "評価額", "評価")),
    ("procedure", ("手続", "必要書類", "申告方法", "書類", "申請")),
)

# ── Caller case-parameter validation bounds ─────────────────────────────────
# input_context is caller-supplied and untrusted: every supported field is
# bounds-checked before use. Violations return status=ERROR naming the FIELD
# only — caller values never round-trip into error logs. Fields this template
# does not define are ignored (the platform may attach its own context keys).
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MIN_YEAR = 1900
_MAX_YEAR = 2100
_MAX_HEIRS = 50


def _validate_iso_date(value: Any) -> str | None:
    """Validate a caller-supplied ISO calendar date within bounded years.

    Returns the canonical YYYY-MM-DD string, or None when the value is not a
    real calendar date in bounds (format-locked, so nothing free-form can pass
    through to state or any rendered surface).
    """
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    if not _MIN_YEAR <= parsed.year <= _MAX_YEAR:
        return None
    return parsed.isoformat()


def _validate_case_context(input_context: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Validate the supported caller case parameters from input_context.

    Supported contract (all optional; absent fields degrade to the baseline —
    the answer simply omits the deadline calendar / deduction figures):
      date_of_death: ISO date string (YYYY-MM-DD) — basis of the statutory
                     10-month filing deadline
      as_of_date:    ISO date string — reference date for the countdown
                     (defaults to the current date downstream)
      heirs_count:   integer 1..50 — statutory-heir count for the basic
                     deduction (相続税法第15条)

    Returns (validated_fields, error). Errors name the field only.
    """
    fields: dict[str, Any] = {"date_of_death": "", "as_of_date": "", "heirs_count": 0}

    for field in ("date_of_death", "as_of_date"):
        if field not in input_context or input_context[field] is None:
            continue
        validated = _validate_iso_date(input_context[field])
        if validated is None:
            return {}, f"{field} must be a calendar date string YYYY-MM-DD ({_MIN_YEAR}..{_MAX_YEAR})"
        fields[field] = validated

    raw_heirs = input_context.get("heirs_count")
    if raw_heirs is not None:
        parsed = _finite_in_range(raw_heirs, 1, _MAX_HEIRS)
        if parsed is None or parsed != int(parsed):
            return {}, f"heirs_count must be an integer between 1 and {_MAX_HEIRS}"
        fields["heirs_count"] = int(parsed)

    return fields, None


class PreProcessNode(FunctionNode):
    """QueryNormalize — sanitize + normalize the question and validate caller data.

    Writes ``normalized_query`` (the field EstateContextExtract reads) plus the
    validated case parameters (``date_of_death`` / ``as_of_date`` /
    ``heirs_count``) taken from ``input_context``. Monetary figures are redacted
    before storage; the intent hint is recorded in ``audit_log``, never a raw
    amount and never a raw caller value.
    """

    # Backbone pre_process gate — VERIFIED_EXTERNAL is correct (external-facing).
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        user_input = state.get("user_input", "")
        if not user_input or not user_input.strip():
            emit_progress(EMPTY_INPUT)
            # A value the caller can correct: nothing downstream runs, and the
            # run COMPLETES carrying the reason so the request can be sent again.
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "EMPTY_INPUT",
                "error_log": ["QueryNormalize: user_input is empty or missing"],
            }

        input_context = state.get("input_context", {}) or {}
        if not isinstance(input_context, dict):
            emit_progress(INPUT_REJECTED)
            # A value the caller can correct: nothing downstream runs, and the
            # run COMPLETES carrying the reason so the request can be sent again.
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": ["QueryNormalize: input_context must be an object"],
            }
        case_fields, err = _validate_case_context(input_context)
        if err:
            emit_progress(INPUT_REJECTED)
            # A value the caller can correct: nothing downstream runs, and the
            # run COMPLETES carrying the reason so the request can be sent again.
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": "INVALID_REQUEST",
                "error_log": [f"QueryNormalize: {err}"],
            }

        clean = _HTML.sub("", user_input).strip()  # sanitize markup
        normalized = _MONEY.sub(_REDACTION, clean)  # redact monetary figures
        intent = self._detect_intent(normalized)

        audit = list(state.get("audit_log", []))
        audit.append(
            json.dumps(
                {
                    "step": "QueryNormalize",
                    "intent_detected": intent,
                    "has_date_of_death": bool(case_fields["date_of_death"]),
                    "has_heirs_count": case_fields["heirs_count"] > 0,
                },
                ensure_ascii=False,
            )
        )

        # S-4: platform audit channel. `audit_log` above is state-local; the
        # trace event is what leaves the agent. Outcome signals only — never the
        # query, the case fields or any monetary figure.
        emit_trace_event(
            "fin_c2_017.query_normalized",
            {
                "template_id": "FIN-C2-017",
                "intent_detected": intent,
                "has_date_of_death": bool(case_fields["date_of_death"]),
                "has_heirs_count": case_fields["heirs_count"] > 0,
                "session_id": state.get("session_id", ""),
            },
            state,
        )
        return {
            "normalized_query": normalized,
            **case_fields,
            "audit_log": audit,
            "status": AgentStatus.SUCCESS.value,
        }

    @staticmethod
    def _detect_intent(text: str) -> str:
        """Lightweight keyword intent hint (audit/logging only)."""
        for intent_type, keywords in _INTENT_KEYWORDS:
            if any(k in text for k in keywords):
                return intent_type
        return "procedure"
