# Template Design Specification — FIN-C2-017 (InheritanceTaxQAAgent)

> Design of record. Node-level code, the full state schema, the KB build and
> the tests follow this document.

## Position in AgentCore Architecture

- **Agent Class**: `InheritanceTaxQAAgent` (`src/graph/graph.py`, `class Graph(AgentBaseGraph)`)
- **Base class**: `AgentBaseGraph` (direct framework inheritance). **Not**
  `AutonomousBaseGraph` — this is a fixed multi-step pipeline, not a self-directed
  think→act→observe loop.
- **Category**: Cat 2 (domain-specific multi-step workflow; the name describes the
  outcome — inheritance-tax Q&A — not a single generic capability).
- **HITL**: not enabled (`hitl.enabled` absent) — no human-in-the-loop review step.
- **Three-Layer Separation**:
  - **State**: flat `TypedDict` (`State(AgentState)`), primitives + JSON-serializable only
    (msgpack-safe; no Pydantic / dataclass / arbitrary objects).
  - **Node**: L1 inheritance via `FunctionNode`; override `execute(self, state: dict) -> dict`
    only, returning **only the fields the node changes** and an `AgentStatus` enum (never a
    plain string).
  - **Graph**: composition via `register_nodes()` filling the fixed `pre_process` / `main` /
    `post_process` slots (`initialize` + `finalize` injected by `super()`).

## Architecture Overview

### Node Configuration

The 6 business steps map onto the fixed 3-slot backbone
(`src/graph/graph.py`).

| Slot | Business node(s) | Responsibility | Input state | Output state | Inherits |
|------|------------------|----------------|-------------|--------------|----------|
| initialize | — | schema_version, session_id, trust_level | user_input | session_id, trust_level | `InitializeNode` (default) |
| pre_process | **QueryNormalize** | Normalize/validate the question, extract intent; **privacy**: strip/refuse raw estate values, never write asset amounts to state or logs | user_input | validated_input, query_intent | `FunctionNode` |
| main | **EstateContextExtract** → **InheritanceTaxKBRetrieve** | Classify query type (threshold / deduction / 路線価 / procedure / deadline), then RAG over the 相続税法 + 国税庁 + 法務局 + 路線価 KBs | validated_input, query_intent | query_type, retrieved_chunks, retrieval_scores | `FunctionNode` (Cat 2 — may wrap an inner subgraph, see DR-3) |
| post_process | **DeadlineCalculate** → **ProcedureChecklistGenerate** → **ResponseValidate** | Compute the 10-month statutory deadline calendar from date of death; LLM-synthesize the 17-step checklist with statutory citations; **output gate**: block individualized tax-liability figures and inject the mandatory 税理士 referral | query_type, retrieved_chunks | deadline_calendar, checklist, answer, blocked | `FunctionNode` |
| finalize | — | response_metadata, total_time_ms | answer | response_metadata | `FinalizeNode` (default) |

### Data Flow

```
START → initialize → pre_process(QueryNormalize) → main(EstateContextExtract → InheritanceTaxKBRetrieve)
              → {route} → post_process(DeadlineCalculate → ProcedureChecklistGenerate → ResponseValidate)
              → finalize → END
                                     ↓ (retry, max 3, on transient retrieval/LLM failure)
                                  pre_process
```

`{route}` after `main`: on low retrieval confidence (`max(retrieval_scores) < score_threshold`)
or `blocked` set by an input-validation refusal, skip synthesis and have `post_process` emit the safe-fallback
answer ("該当する情報が見つかりませんでした" + 税理士 referral). Otherwise proceed to full synthesis.

### State Definition (authoritative schema: `src/schemas/state.py`)

Flat `TypedDict` extending `AgentState`. Inherited: `user_input`, `status`, `session_id`,
`node_history`, `error_log`, `hitl_*`. Agent-specific (preview):

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| validated_input | str | normalized question text | yes |
| query_intent | str | coarse intent label | yes |
| query_type | str | one of: threshold / deduction / rosenka / procedure / deadline | yes |
| retrieved_chunks | list[dict] | top-k KB records (id, text, source, citation) — JSON-serializable | yes |
| retrieval_scores | list[float] | per-chunk scores for the route gate | yes |
| deadline_calendar | dict | computed milestone dates keyed by step | no |
| checklist | list[dict] | 17-step procedure items with statutory citations | no |
| answer | str | final JSON-encoded answer string (status, body, citations, referral) | yes |
| blocked | bool | input/output-gate short-circuit flag (no retrieval / no synthesis) | no |
| error_code | str \| None | reason code for a correctable rejection; makes every later node pass through untouched. Internal only — not part of the response envelope | no |

**Constraints**: flat TypedDict only; no JWT/API keys/credentials in state; `InvocationContext`
via `config["configurable"]` only; **no estate monetary values persisted**.

## KB Design

| KB | Source | Access | Retrieval |
|----|--------|--------|-----------|
| 相続税法 corpus | e-Gov 法令 (public) | free | dense (legal prose) |
| 国税庁 guidance | 国税庁 publications (public) | free | dense |
| 法務局 estate procedure | 法務局 (public) | free | dense |
| 路線価 reference | 国税庁 annual 路線価 (public) | free | **hybrid** (exact code/地番 match) |

- `路線価` lookups require exact-match on area/road codes → enable `hybrid_search` for that
  collection; legal/guidance text uses dense retrieval.
- Output affects financial/legal decisions → `temperature: 0.0`, `score_threshold ≈ 0.75`,
  `top_k ≥ 8`, larger `max_tokens` for multi-citation answers.
- All sources are public and free — **no licensing blockers**.

## Security Design (5-layer)

| Layer | Implementation | This template |
|-------|----------------|---------------|
| Trust gate | `required_trust_level` (enforced in `BaseNode.__call__`) | `VERIFIED_EXTERNAL` on the pre_process gate; QueryNormalize refuses/strips raw estate monetary values; **no asset amounts written to state or trace logs** |
| Input gate | `_security_gate_input()` | PII (names, addresses, マイナンバー) detected/redacted on input; never echoed |
| Output gate | module-level `_security_gate_output()` (`src/nodes/post_process_node.py`) + the framework credential scan | **Blocks individualized tax-liability figures**; **injects the mandatory 税理士 referral**; withholds credential-carrying drafts; enforces the 1,000-yen precision grid on every monetary token |
| Audit | `emit_trace_event()` | structured audit on every node; no estate values in the trace |
| Credential handling | `__init_subclass__` + output credential scan | framework credential-pattern detection (automatic) |

### What the precision grid is, and what it is not

The output gate snaps every **monetary** token onto the 1,000-yen grid. A monetary
token is identified by FORM (comma-grouped, or an unformatted run of 5+ digits) and
by CURRENCY CONTEXT (a currency symbol, or a 3-letter code, on either side of the
value). It is never identified by magnitude.

Three kinds of token this template renders are explicitly **outside** the grid,
because they are not amounts and rewriting them corrupts the answer:

| Not on the grid | Example from this template | Why |
|---|---|---|
| Decimal fractions of a non-amount | `8.512345` 路線価 correction ratio · `99.99999%` 配偶者の税額軽減 rate | The fraction is a standalone 5+-digit run in its own right. Each value alternative absorbs its fraction into the SAME token (`(?:\.\d+\|(?!\.\d))`, not `(?:\.\d+)?` — the optional form backtracks and re-matches the integer part alone). An off-grid amount in explicit currency context still snaps, as ONE number: `JPY 1234.56` → `JPY 1,000`. |
| Identifiers | `2026-11-15` filing deadline · `nta-4152` / `NTA-4152` `kb_citations` · `https://www.nta.go.jp/…/4152.htm` | The render alphabet includes `-`, `_`, `/`, `:` and `.`, so those characters guard the token boundary. Only an **ISO-4217** code may bind to a value it is ATTACHED to, so `JPY-9999` snaps while the 国税庁 citation `NTA-4152` survives. A whitespace-SEPARATED 3-letter code stays unrestricted — the gate fails safe on the ambiguous case. |
| Structure | `3. 相続税の申告書を…` checklist step numbers | The marker-to-value delimiter is horizontal whitespace plus at most ONE newline, never a paragraph break, so a 3-letter code ending one block cannot bind to the number opening the next. |

Japanese word characters (万 / 円 / 条 / 日 / 人) are held by the `\b` after the
marker-then-value branch, which the ASCII identifier guards do not replace —
without it `（3000万円+600万円×法定相続人の数）` matches as 円-marker + `+600`.
Pinned both directions in `tests/unit/test_gate_decimal_and_identifiers.py`.

`required_trust_level` value is one of the three valid enums (`ANONYMOUS` / `VERIFIED_EXTERNAL` /
`INTERNAL`) — here `VERIFIED_EXTERNAL`. The agent gives **guidance with
statutory citations**, never a personalized tax calculation, and always refers to a 税理士.

The gate and the referral invariant apply to a SYNTHESIZED answer — the path that quotes
statutes, computes the deadline calendar and renders the checklist. The body returned for a
correctable rejection is outside them, deliberately: it is a fixed module constant built from no
retrieved chunk and no caller text, it states only what to correct, and it contains no guidance,
no figure and no statutory claim — so there is nothing for the gate to block and no advice for
the referral to qualify. Only a synthesized answer reaches the gate.

## Framework Utilization

- [x] `InvocationContext` (correlation_id, session_id, permissions, credential handle)
- [x] Audit trail (`audit_log` state entries + framework trace events)
- [x] Retry/timeout configuration (`max_retry` / `timeout_s`, `config/config.yaml`)
- [x] `SecurityViolationError`
- [x] `_security_gate_input()` (PII detection — mandatory)
- [x] `_security_gate_output()` (content safety — mandatory)

### Composition Pattern

- **Pattern**: Cat 2 fixed 3-slot backbone (this revision). The `main` slot runs
  EstateContextExtract → InheritanceTaxKBRetrieve sequentially within one `FunctionNode`.
- **Invoke chain**: agent is invoked via `.invoke()` (never `.run()`).
- **Error propagation**: nodes return a status + `error_log` rather than raising to the caller.
  Two outcomes are distinguished. A rejection the caller can CORRECT — an empty question, a
  non-object `input_context`, an out-of-contract case field — does not terminate the run: nothing
  downstream executes, but the run completes with `AgentStatus.SUCCESS` carrying a reason code in
  `error_code`, and `post_process` renders the fixed sentence for that code as the response body,
  so the caller can fix the value and send the request again on the same conversation instead of
  receiving only an exception type. `error_code` is a State field, NOT part of the response
  envelope — the reason reaches the caller as the body sentence only, and it names what to correct
  without echoing the rejected value or an internal field path (those stay in `error_log`). Once
  `error_code` is set, every later node returns immediately, so a declined request never reaches
  retrieval or synthesis, and the degraded response carries no calculated figure or checklist.
  A failure the caller cannot influence — such as an upstream contract break reaching
  `main` with an empty normalized query — still returns `AgentStatus.ERROR`.

## Import Isolation Confirmation
- [x] Template does not import the platform-internal SDK (agenticstar.* packages)
- [x] Import targets: `framework/` and `shared/` only (no `agents/base/`)

## Design Decision Record

| ID | Decision | Option A | Option B | Chosen | Rationale |
|----|----------|----------|----------|--------|-----------|
| DR-1 | L1 base type | `AgentBaseGraph` | `AutonomousBaseGraph` | **A** | Fixed pipeline; no autonomous loop |
| DR-2 | Pattern label (`base_type`) | `RAGAgent` (VectorRAG) | `DocGenerationAgent` | **A (RAGAgent) — recommended** | Core is retrieval-augmented Q&A over legal KBs with cited answers; the checklist/calendar are structured *outputs* of that, not free-form document generation. **The design intent is VectorRAG; `config/agent.yaml` currently says `DocGenerationAgent`.** |
| DR-3 | Node slot mapping | 6 nodes → 3 fixed slots (group EstateContextExtract+Retrieve in `main`, the 3 synth steps in `post_process`) | `main` = `GraphNode` wrapping an inner `domain_workflow_graph.py` with all 6 as discrete nodes | **A** | Keeps the release simple; B (inner subgraph, the Cat 2 "ideal") is a documented future refactor if per-node observability is needed |

> **Open item:** DR-2 (RAGAgent vs DocGenerationAgent) — the pattern label in
> `config/agent.yaml` should be reconciled with the design.
