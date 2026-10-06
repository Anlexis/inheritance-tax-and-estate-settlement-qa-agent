"""AgentCore Platform v1.0"""

# post_process slot —
#   DeadlineCalculate          → deadline_info
#   BasicDeductionCalculate    → deduction_info
#   ProcedureChecklistGenerate → procedure_draft (grounded in kb_chunks)
#   ResponseValidate           → final_response / formatted_output
#
# ResponseValidate is the PRIMARY domain output boundary here:
#   - hard block individualized tax-liability calculation requests (scope-out)
#   - ALWAYS inject the 税理士 referral statement
#   - inject a deadline warning when the statutory deadline is within reach
#   - cite 相続税法 articles from retrieved chunks
#   - never echo the raw question; credential patterns withhold the answer
# The module-level _security_gate_output() additionally enforces the documented
# external-output schema (verbatim-embedding redaction + the monetary
# precision grid) on every response — see the schema comment below.
# The gate is a module-level function rather than an _extra_security_gate_*
# instance method, keeping the hook stateless.

import json
import re
from datetime import date
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event
from src.services.failure_message import EMPTY_INPUT, INPUT_REJECTED, INVALID_VALUE, TOO_LONG

_STATUTE_MONTHS = 10  # 相続税法第27条 — 10-month filing deadline

# 相続税法第15条 — statutory basic deduction: 30,000,000円 + 6,000,000円 per heir.
_DEDUCTION_BASE_YEN = 30_000_000
_DEDUCTION_PER_HEIR_YEN = 6_000_000
_MAX_HEIRS = 50

_TAX_ADVISOR_REFERRAL = "※ 本回答は一般的な手続きのご案内です。個別の税額計算や具体的な判断は税理士にご相談ください。"
_SCOPE_OUT = (
    "個別の相続税額の計算はこのサービスでは行えません。" "一般的な手続き・期限・必要書類についてはご案内できます。"
)
_SCHEMA_NOTE = "（金額は1,000円単位表示）"

# Individualized tax-liability calculation requests → scope-out (do NOT calculate).
_LIABILITY_PATTERNS = (
    re.compile(r"(税額|納税額|相続税).{0,8}(いくら|計算|算出|教え)"),
    re.compile(r"(私|うち|当方|自分).{0,10}(税額|いくら払|納める|相続税)"),
    re.compile(r"how much.*(tax|owe)", re.IGNORECASE),
    re.compile(r"calculate.*(my|our).*(tax|liabilit)", re.IGNORECASE),
)
_ARTICLE_RE = re.compile(r"相続税法第\s*\d+条(?:の\d+)?")

# Domain credential patterns — a leaked secret-looking token anywhere in the
# drafted answer withholds the answer entirely (graceful BLOCKER notice)
# BEFORE the framework's own output gate would hard-fail the node.
_CREDENTIAL_PATTERNS = (
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),
    re.compile(r"eyJ[a-zA-Z0-9._-]{10,}"),
    re.compile(r"AKIA[A-Z0-9]{16}"),
    re.compile(r"Bearer\s+[a-zA-Z0-9._-]{20,}"),
    re.compile(r"""(?i)(api[_-]?key|secret|password|token)\s*=\s*['"][^'"]{8,}['"]"""),
)
_BLOCKER_NOTICE = "BLOCKER: 出力に機密情報パターンを検出したため回答を保留しました。\n\n" + _TAX_ADVISOR_REFERRAL

# Minimal master-checklist subset by query_type (statutory citations included).
_CHECKLIST = {
    "deadline": ["申告期限（相続開始を知った日の翌日から10か月以内）を確認（相続税法第27条）"],
    "tax_threshold": ["基礎控除額を確認（3000万円+600万円×法定相続人の数）（相続税法第15条）"],
    "deduction": ["配偶者の税額軽減・各種控除の適用要件を確認（相続税法第19条の2 ほか）"],
    "route_price": ["宅地の評価方法（路線価方式／倍率方式）を確認"],
    "procedure": ["必要書類（戸籍謄本・遺産分割協議書等）を準備し所轄税務署へ申告"],
}

# State fields that must NEVER be embedded verbatim in external responses.
# user_input is the caller's RAW question (pre-redaction — it may contain
# personal estate amounts); the response is built from KB + statutory content,
# so a verbatim embedding is a leak, not a feature.
_BLOCKED_FIELDS = frozenset({"user_input"})

# Approved external precision (the response RENDERS on this grid, the gate
# ENFORCES it — the schema note above is printed wherever money renders).
_EXTERNAL_ROUND_UNIT = 1000
# EXPLICIT output schema — monetary values are identified by FORM and by
# CURRENCY CONTEXT, never by magnitude:
#   form:    comma-grouped numbers (9,999 / 1,234,567 — the renderer formats
#            every monetary value with "{:,.0f}") and unformatted runs of 5+
#            digits (a rendering-regression leak);
#   context: any bare 1-4 digit number associated with a currency marker is
#            monetary even though short — SYMMETRICALLY: a 3-letter uppercase
#            code or a currency symbol (incl. fullwidth ￥ and 円/₩), before or
#            after the value. A code ATTACHED to the value must be a real
#            ISO-4217 code ("JPY-9999" snaps); a code SEPARATED by whitespace
#            may be any 3-letter uppercase word, so the gate still fails SAFE
#            on the ambiguous case ("SKF 6205" snaps). The attached
#            restriction is what keeps this template's own citation IDs
#            intact — "NTA-4152" is a 国税庁 reference, not 国税庁 minus 4,152 yen.
#            A currency SYMBOL (incl. fullwidth ￥ and 円/₩) is a marker in
#            either position, attached or separated, signed or unsigned.
# Structural tokens stay untouched: horizons ("90d" — lowercase suffix),
# bare counts ("3 breach(es)"), years without currency adjacency
# ("year 2026", "in 2026"), ISO dates ("2026-11-15") and citation IDs.
# ALL matched tokens, at ANY magnitude, must sit on the rounding grid;
# off-grid = a full-precision value derived from protected fields,
# snapped + audited.
# Branch order matters: currency-context branches first, then form-based.
_SYMBOL = r"[¥￥$€£円₩]"
_ANY_CODE = r"[A-Z]{3}(?![A-Za-z])"
# ISO-4217 codes only — the closed list that may bind to a value it is ATTACHED
# to. An identifier list could never be closed; a currency list is.
_ISO_CODE = (
    r"(?:JPY|USD|EUR|GBP|CNY|CNH|KRW|AUD|CAD|CHF|HKD|SGD|TWD|THB|INR|IDR|MYR|PHP|VND"
    r"|BRL|MXN|SEK|NOK|DKK|PLN|CZK|TRY|NZD|RUB|ZAR|SAR|AED|ILS)(?![A-Za-z])"
)

# Delimiter between a currency marker and its value: horizontal whitespace and at
# most ONE newline — never a paragraph break. A plain `\s*` spans blank lines, so a
# 3-letter uppercase word ending a line would bind to the number that opens the next
# block and rewrite it. In THIS template the synthesised checklist is a numbered
# list, so "…タックスアンサー PDF\n\n3. 相続税の申告書を…" became
# "…タックスアンサー PDF\n\n0. 相続税の申告書を…" — the gate renumbered a step of a
# statutory procedure checklist. Every enumerated leak form (spaces, tabs, single
# newline, signed, symmetric, comma-grouped) still matches.
_GATE_DELIM = r"[ \t]*(?:\n[ \t]*)?"
# The same delimiter with at least one whitespace character present.
_GATE_DELIM_WS = r"(?:[ \t]+|[ \t]*\n[ \t]*)"

# Marker BEFORE the value: any 3-letter uppercase word or a symbol when separated
# by whitespace; only an ISO-4217 code or a symbol when attached.
_CURRENCY_MARKER = rf"(?:(?:{_ANY_CODE}|{_SYMBOL}){_GATE_DELIM_WS}|(?:{_ISO_CODE}|{_SYMBOL}){_GATE_DELIM})"
# Marker AFTER the value, same rule mirrored.
_CURRENCY_MARKER_POST = rf"(?:{_GATE_DELIM_WS}(?:{_ANY_CODE}|{_SYMBOL})|{_GATE_DELIM}(?:{_ISO_CODE}|{_SYMBOL}))"

# Every value alternative absorbs its decimal fraction into the SAME token.
# Without that, two separate things go wrong:
#   - the fraction of "8.512345" is a standalone 5+-digit run in its own right,
#     so the form-based branch latches onto it and rewrites a 路線価 correction
#     ratio into "8.512,000" — a number the document never contained; the same
#     defect turned a 配偶者の税額軽減 rate of "99.99999%" into "99.100,000%";
#   - in currency context the integer part snaps while the fraction dangles
#     ("JPY 1234.56" -> "JPY 1,000.56"), a value that is neither the true figure
#     nor on the grid.
# The fix is NOT to exempt decimals from the grid: an off-grid amount in explicit
# currency context still snaps, as ONE number ("JPY 1234.56" -> "JPY 1,000"),
# while a ratio or percentage — never a monetary token — is left byte-identical.
#
# The `(?!\.\d)` arm is what makes the absorption stick. A plain `(?:\.\d+)?`
# lets the engine backtrack out of the fraction and re-match the integer part
# alone whenever the text right after the fraction fails the trailing guard, so
# "JPY 1234.56m" would go back to matching "JPY 1234" and the dangling-fraction
# bug returns. Either the fraction is taken whole, or there is none there.
_VAL_FRACTION = r"(?:\.\d+|(?!\.\d))"

# Identifier guards, widened to THIS template's render alphabet. The generic
# guard class [A-Za-z0-9-] is not enough, because the identifiers this agent
# renders are also delimited by `_`, `/` and `:`:
#
#   2026-11-15                              the statutory filing deadline (ISO date)
#   nta-4152 / NTA-4152 / law-15            kb_citations — the `citation_id` of the
#                                           KB record, whose format is free-form
#   sozoku_4152                             the underscore-joined JSONL variant
#   https://www.nta.go.jp/taxes/4152.htm    a 国税庁 HTML source rendered as the
#                                           citation itself (the corpus is built
#                                           from PDF / HTML material)
#   参考(KB): <chunk> / <chunk>              the checklist's own KB join
#
# Without those characters in the class a citation whose numeric part is a 5+-digit
# run is read as a monetary figure and rewritten — the agent would cite a 国税庁
# reference that does not exist, in an answer whose entire value is that the
# citation is checkable. `.` is in the LEADING guard only: in the trailing guard it
# would let an amount that ends a sentence escape the grid ("控除額は JPY 9999。").
#
# The guards are an ASCII class, so they are NOT a substitute for the `\b` after
# the marker-then-value branch: this template renders Japanese, where 万 / 円 / 条 /
# 日 / 人 are word characters. Drop the `\b` and the checklist's own statutory
# formula breaks — "（3000万円+600万円×法定相続人の数）" matches as 円-marker + "+600"
# and renders "（3000万円+1,000万円×…）", a misquoted 相続税法第15条. `\b` fails
# between "0" and "万"; the ASCII trailing guard does not. Keep both.
_LEAD_GUARD = r"(?<![A-Za-z0-9_/:.\-])"
_TRAIL_GUARD = r"(?![A-Za-z0-9_/:\-])"

_NUM_TOKEN_RE = re.compile(
    _LEAD_GUARD
    # marker THEN value: "JPY 9999", "JPY  -9999", "JPY\t9999", "¥9999", "USD\n+9999".
    # The value alternatives accept a comma-grouped form FIRST: the regex is
    # leftmost-first, so without it "JPY 1,234" would match as marker + "1"
    # (mangling the number on the snap) instead of as the whole grouped value
    # — an on-grid "JPY 1,000" must stay byte-identical, and an off-grid
    # "JPY 1,234" must snap as 1234, not as 1.
    + rf"(?:(?P<pre>{_CURRENCY_MARKER})"
    rf"(?P<val_after>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{1,4}}{_VAL_FRACTION})\b"
    # value THEN marker: "9999 JPY", "-9999\tJPY", "9999円", "+9999  $"
    rf"|(?P<val_before>[+-]?\d{{1,4}}{_VAL_FRACTION})(?P<post>{_CURRENCY_MARKER_POST})"
    # form-based, standalone at any magnitude: comma-grouped or 5+-digit runs
    rf"|(?P<val_form>[+-]?\d{{1,3}}(?:,\d{{3}})+{_VAL_FRACTION}|[+-]?\d{{5,}}{_VAL_FRACTION}))" + _TRAIL_GUARD
)


def _enforce_precision(result: str) -> tuple[str, int]:
    """Snap every monetary-form token onto the approved external grid.

    Returns (sanitised_result, redaction_count). A redaction means a value
    derived from protected data reached the external surface at full
    precision — the gate rounds it onto the approved grid. The currency
    marker, the original delimiter whitespace, and the explicit sign of the
    original token are all preserved on the snapped replacement.
    """
    redactions = 0

    def _snap(match: re.Match[str]) -> str:
        nonlocal redactions
        pre = match.group("pre") or ""
        post = match.group("post") or ""
        token = match.group("val_after") or match.group("val_before") or match.group("val_form")
        # float(), not int(): the token may carry a decimal fraction, and the
        # whole amount — not just its integer part — is what sits on the grid.
        value = float(token.replace(",", ""))  # float() understands leading +/-
        if value % _EXTERNAL_ROUND_UNIT == 0:
            return match.group(0)
        redactions += 1
        snapped = round(value / _EXTERNAL_ROUND_UNIT) * _EXTERNAL_ROUND_UNIT
        plus = "+" if token.startswith("+") and snapped >= 0 else ""
        return f"{pre}{plus}{snapped:,d}{post}"

    return _NUM_TOKEN_RE.sub(_snap, result), redactions


def _security_gate_output(result: str, state: dict[str, Any]) -> str:
    """Enforce the external-output boundary on the drafted response.

    Module-level function (NOT an instance method). Three ordered layers:
    a credential-looking token anywhere withholds the answer (BLOCKER
    notice); verbatim blocked-field embedding is replaced with [REDACTED];
    then every monetary-form token is snapped onto the approved precision
    grid (_enforce_precision). Returns the sanitised result.
    """
    if any(p.search(result) for p in _CREDENTIAL_PATTERNS):
        emit_trace_event(
            "fin_c2_017.credential_withheld",
            {"template_id": "FIN-C2-017", "session_id": state.get("session_id", "")},
            state,
        )
        return _BLOCKER_NOTICE

    sanitised = result
    embedded_fields = []
    for field in _BLOCKED_FIELDS:
        val = state.get(field)
        if val and isinstance(val, str) and len(val) > 10 and val in sanitised:
            sanitised = sanitised.replace(val, "[REDACTED]")
            embedded_fields.append(field)
    if embedded_fields:
        emit_trace_event(
            "fin_c2_017.verbatim_redaction",
            {
                "template_id": "FIN-C2-017",
                "fields": embedded_fields,
                "session_id": state.get("session_id", ""),
            },
            state,
        )

    sanitised, precision_redactions = _enforce_precision(sanitised)
    if precision_redactions:
        emit_trace_event(
            "fin_c2_017.precision_redaction",
            {
                "template_id": "FIN-C2-017",
                "redaction_count": precision_redactions,
                "session_id": state.get("session_id", ""),
            },
            state,
        )

    return sanitised


# Reason code -> the sentence the caller reads. A code with no entry falls
# back to the generic one rather than leaking the code itself.
_DEGRADED_MESSAGES = {
    "EMPTY_INPUT": EMPTY_INPUT,
    "QUESTION_TOO_LONG": TOO_LONG,
    "INVALID_REQUEST": INVALID_VALUE,
}


class PostProcessNode(FunctionNode):
    """post_process: deadline + deduction calc, checklist synthesis, response validation."""

    # Runs behind the pre_process gate — the external trust boundary is there.
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.ANONYMOUS

    def __init__(self, llm_client: Any | None = None) -> None:
        super().__init__()
        self._llm = llm_client

    def execute(self, state: dict[str, Any]) -> dict[str, Any]:
        # A run declined upstream has nothing to format. Render the reason as
        # the caller-facing body and carry the marker onward.
        marker = state.get("error_code")
        if marker:
            message = _DEGRADED_MESSAGES.get(marker, INPUT_REJECTED)
            emit_trace_event("post_process_degraded", {"reason": marker}, state)
            return {
                "status": AgentStatus.SUCCESS.value,
                "error_code": marker,
                "output": message,
                "formatted_output": message,
                "final_response": message,
            }
        query = state.get("normalized_query") or state.get("user_input") or ""
        deadline_info = self._deadline_calculate(state)
        deduction_info = self._deduction_calculate(state)
        procedure_draft = self._procedure_checklist(state)
        if any(p.search(procedure_draft) for p in _CREDENTIAL_PATTERNS):
            # Withhold a credential-carrying draft entirely — nothing derived
            # from it may reach state or the caller.
            procedure_draft = ""
            final_response = _security_gate_output(_BLOCKER_NOTICE, state)
        else:
            final_response = self._response_validate(query, state, procedure_draft, deadline_info, deduction_info)

        audit = list(state.get("audit_log", []))
        audit.append(
            json.dumps(
                {
                    "step": "ResponseValidate",
                    "scope_out": final_response.startswith(_SCOPE_OUT[:10]),
                    "deadline_available": bool(deadline_info.get("available")),
                    "deduction_available": bool(deduction_info.get("available")),
                },
                ensure_ascii=False,
            )
        )
        # S-4: the withholding/redaction events are emitted inside
        # `_security_gate_output`, which is a MODULE-LEVEL function by design (see
        # its docstring) — so the response-boundary outcome is recorded here, on
        # execute()'s own path. Outcome signals only: no drafted response text, no
        # monetary figure and no case field leaves on this channel.
        emit_trace_event(
            "fin_c2_017.response_validated",
            {
                "template_id": "FIN-C2-017",
                "scope_out": final_response.startswith(_SCOPE_OUT[:10]),
                "withheld": final_response == _BLOCKER_NOTICE,
                "deadline_available": bool(deadline_info.get("available")),
                "deduction_available": bool(deduction_info.get("available")),
                "session_id": state.get("session_id", ""),
            },
            state,
        )
        return {
            "deadline_info": deadline_info,
            "deduction_info": deduction_info,
            "procedure_draft": procedure_draft,
            "final_response": final_response,
            "formatted_output": final_response,
            "audit_log": audit,
            "status": AgentStatus.SUCCESS.value,
        }

    # ── DeadlineCalculate ────────────────────────────────────
    def _deadline_calculate(self, state: dict[str, Any]) -> dict[str, Any]:
        parsed = self._parse_date(state.get("date_of_death"))
        if not parsed:
            return {"available": False, "note": "deadline calendar requires date of death"}
        deadline = self._add_months(parsed, _STATUTE_MONTHS)
        info: dict[str, Any] = {
            "available": True,
            "filing_deadline": deadline.isoformat(),
            "statute": "相続税法第27条（申告期限：相続開始を知った日の翌日から10か月以内）",
        }
        today = self._parse_date(state.get("as_of_date")) or date.today()
        days = (deadline - today).days
        info["days_remaining"] = days
        info["within_30_days"] = 0 <= days <= 30
        info["deadline_passed"] = days < 0
        return info

    # ── BasicDeductionCalculate ──────────────────────────────
    def _deduction_calculate(self, state: dict[str, Any]) -> dict[str, Any]:
        """Statutory basic deduction (相続税法第15条) from the validated heir count.

        This is the published statutory THRESHOLD formula — general information
        the checklist itself instructs users to confirm — never an
        individualized tax-liability calculation (those are scoped out).
        """
        heirs = state.get("heirs_count", 0)
        if not isinstance(heirs, int) or isinstance(heirs, bool) or not 1 <= heirs <= _MAX_HEIRS:
            return {"available": False, "note": "basic deduction requires a validated heirs_count"}
        total_yen = _DEDUCTION_BASE_YEN + _DEDUCTION_PER_HEIR_YEN * heirs
        return {
            "available": True,
            "heirs_count": heirs,
            "total_yen": total_yen,
            "statute": "相続税法第15条（基礎控除：3,000万円+600万円×法定相続人の数）",
        }

    # ── ProcedureChecklistGenerate ───────────────────────────
    def _procedure_checklist(self, state: dict[str, Any]) -> str:
        query_type = state.get("query_type", "procedure")
        kb_chunks = [c for c in (state.get("kb_chunks") or []) if isinstance(c, str)]
        base_steps = _CHECKLIST.get(query_type, _CHECKLIST["procedure"])

        if self._llm is not None and kb_chunks:
            prompt = (
                "次の根拠KBに基づき、相続手続きのチェックリストを日本語で作成してください。"
                "根拠に無い手順は追加しないでください。条番号があれば併記してください。\n"
                f"質問種別: {query_type}\n根拠KB:\n" + "\n".join(f"- {c}" for c in kb_chunks)
            )
            try:
                generated = self._llm.complete(prompt)
            except Exception:
                generated = ""
            if isinstance(generated, str) and generated.strip():
                return generated.strip()

        # Deterministic fallback: built-in steps grounded by any retrieved chunks.
        lines = list(base_steps)
        if kb_chunks:
            lines.append("参考(KB): " + " / ".join(kb_chunks[:3]))
        return "\n".join(f"- {s}" for s in lines)

    # ── ResponseValidate ─────────────────────────────────────
    def _response_validate(
        self,
        query: str,
        state: dict[str, Any],
        procedure_draft: str,
        deadline_info: dict[str, Any],
        deduction_info: dict[str, Any],
    ) -> str:
        # Hard block: individualized tax-liability calculation requests.
        if any(p.search(query) for p in _LIABILITY_PATTERNS):
            return _security_gate_output(f"{_SCOPE_OUT}\n\n{_TAX_ADVISOR_REFERRAL}", state)

        parts: list[str] = [procedure_draft]

        # Statutory basic deduction — computed from the validated heir count.
        # Rendered in raw yen with comma grouping: the figure is always a
        # multiple of 1,000,000, so it sits on the approved 1,000 grid.
        if deduction_info.get("available"):
            parts.append(
                "基礎控除額の目安: {total:,d}円"
                "（3,000万円 + 600万円 × 法定相続人{heirs}人・相続税法第15条）{note}".format(
                    total=deduction_info["total_yen"],
                    heirs=deduction_info["heirs_count"],
                    note=_SCHEMA_NOTE,
                )
            )

        # Mandatory deadline warning when a deadline is known.
        if deadline_info.get("available"):
            if deadline_info.get("deadline_passed"):
                parts.append(
                    f"【期限注意】申告期限（{deadline_info['filing_deadline']}）を過ぎています。至急ご対応ください。"
                )
            elif deadline_info.get("within_30_days"):
                parts.append(
                    f"【期限警告】申告期限（{deadline_info['filing_deadline']}）まで残り{deadline_info['days_remaining']}日です。"
                )
            else:
                parts.append(f"申告期限の目安: {deadline_info['filing_deadline']}（相続税法第27条）")

        # Citations: 相続税法 articles found in the retrieved chunks + citation IDs.
        articles = sorted(
            {
                a.replace(" ", "")
                for c in (state.get("kb_chunks") or [])
                if isinstance(c, str)
                for a in _ARTICLE_RE.findall(c)
            }
        )
        citations = [c for c in (state.get("kb_citations") or []) if isinstance(c, str)]
        if articles or citations:
            refs = "、".join(articles + [c for c in citations if c not in articles])
            parts.append(f"根拠: {refs}")

        # Mandatory 税理士 referral (always present).
        parts.append(_TAX_ADVISOR_REFERRAL)

        return _security_gate_output("\n\n".join(p for p in parts if p), state)

    # ── date helpers ─────────────────────────────────────────
    @staticmethod
    def _parse_date(value: Any) -> date | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            y, m, d = (int(x) for x in value.strip().split("-"))
            return date(y, m, d)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _add_months(d: date, months: int) -> date:
        month_index = d.month - 1 + months
        year = d.year + month_index // 12
        month = month_index % 12 + 1
        # clamp day to the target month's last valid day
        day = d.day
        for last in (31, 30, 29, 28):
            try:
                return date(year, month, min(day, last))
            except ValueError:
                continue
        return date(year, month, 28)
