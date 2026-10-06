# Test Specification — FIN-C2-017
# InheritanceTaxQAAgent (Cat 2, direct AgentBaseGraph inheritance)

## Test Strategy
- Coverage target: **80%**
- Test types: Unit / Integration / Proof-of-Boundary
- Test files: `tests/unit/`, `tests/proof_of_boundary/` (TC-IDs match test docstrings)
- **Output-field assertions are mandatory** — verify the exact output state fields a
  node writes (`query_type`, `kb_chunks`, `final_response`, …), not just `status`.
- Pipeline under test: `initialize → pre_process (QueryNormalize) → main
  (EstateContextExtract + InheritanceTaxKBRetrieve) → post_process (DeadlineCalculate +
  ProcedureChecklistGenerate + ResponseValidate) → finalize`.
- Implemented test files: `tests/unit/` (per-node units, trust gate, output gate,
  framework compliance TC-06/07), `tests/proof_of_boundary/` (import isolation,
  state safety, invoke order PB-6, output-gate invariants, HITL stub PB-7, and the
  end-to-end `/invoke` suite `test_invoke_e2e.py`).

---

## Section 1 — Framework Compliance Tests (mandatory)

| TC-ID | Test | Expected Result | Result |
|-------|------|----------------|--------|
| TC-01 | State is a flat `TypedDict` (`State(AgentState)`); no Pydantic/dataclass | AST/type check pass; all fields primitives/JSON | PASS (`test_state_safety`) |
| TC-02 | Invalid input → rejection path | `PreProcessNode` empty `user_input`, non-object `input_context`, or an out-of-contract case field → the request is refused — no `normalized_query` is published, and `error_log` names the FIELD, never the rejected value. This is a rejection the caller can correct, so the run COMPLETES (`AgentStatus.SUCCESS` + a reason code in `error_code`) and `post_process` renders the reason as the response body — the caller can fix the value and send again on the same conversation. Rejections the caller cannot correct by rewording still terminate with `AgentStatus.ERROR` | PASS (`test_pre_process_node`) |
| TC-03 | No JWT/credential in State | credential-pattern AST scan of `state.py` = 0 | PASS (`test_state_safety`) |
| TC-04 | `InvocationContext` via `config["configurable"]` only | not stored in State; passed via `config` | PASS (`test_state_safety`) |
| TC-05 | `emit_trace_event` / `audit_log` recorded | each node appends an audit entry (`step`, `query_type`/`kb_sources_used`) | PASS (`test_output_gate`, node units) |
| TC-06 | `_security_gate_input()` non-bypassable | overriding the default input gate raises at class definition | PASS (`test_framework_compliance_tc06_tc07`) |
| TC-07 | `_security_gate_output()` non-bypassable | overriding the default output gate raises at class definition | PASS (`test_framework_compliance_tc06_tc07`) |
| TC-08 | `required_trust_level` is a valid `TrustLevel` enum | one of `ANONYMOUS`/`VERIFIED_EXTERNAL`/`INTERNAL`; insufficient trust → refused before execute() | PASS (`test_trust_gate`) |

---

## Section 2 — Proof-of-Boundary Tests (mandatory)

| PB-ID | Boundary | Expected | Assertions | Result |
|-------|----------|----------|-----------|--------|
| PB-1 | Node → trace/audit | no silent failures | each node's return appends to `audit_log` (string entries) | PASS (`test_output_gate`) |
| PB-2 | State serialization | primitives/JSON only | every `State` field type ∈ `str/int/float/bool/list/dict`; inherits `AgentState` not BaseModel | PASS (`test_state_safety`) |
| PB-3 | L1 → external KB | real retrieval | MVP is in-memory term-overlap; live vector store is a planned follow-up → **N/A for MVP** | N/A |
| PB-4 | Import isolation | 0 Level-0 imports | no `from agenticstar`/`import agenticstar`; no cross-template imports in `src/` | PASS (`test_import_isolation`) |
| PB-5 | Checkpoint safety | no JWT/Pydantic/InvocationContext in checkpoint | `state.py` has no credential fields / no `InvocationContext` annotation | PASS (`test_state_safety`) |
| PB-6 | Invoke execution order | initialize→pre_process→main→post_process→finalize | per-node gate order via spy + full-graph node_history | PASS (`test_pb_invoke_order`) |
| PB-7 | HITL interrupt propagation | — | **N/A** — `hitl.enabled` is not set for FIN-C2-017 | N/A |

---

## Section 3 — Domain-Specific Tests (FIN-C2-017)

| TC-ID | Test | Expected | Result |
|-------|------|----------|--------|
| TC-D01 | **Scope-out**: individualized tax-liability calc request | scope-out message, **no calculation**; 税理士 referral present | PASS (`test_output_gate`, `test_invoke_e2e`) |
| TC-D02 | **Referral invariant**: every synthesized answer | `final_response` always contains the 税理士 referral string (incl. scope-out path). The body returned for a correctable rejection is not a synthesized answer — it carries no guidance, figure or statutory claim — so it is outside this invariant | PASS (`test_output_gate`) |
| TC-D03 | **Monetary redaction**: estate value in input (e.g. `3億円`) | redacted to `[金額]` in `normalized_query`; no monetary amount in `audit_log`/`kb_chunks` | PASS (`test_pre_process_node`) |
| TC-D04 | DeadlineCalculate: `date_of_death` provided | `deadline_info.filing_deadline` = date_of_death + 10 months (相続税法第27条); `within_30_days`/`deadline_passed` flags correct | PASS (`test_post_process_node`, `test_invoke_e2e`) |
| TC-D05 | EstateContextExtract: `路線価` query | `query_type == "route_price"` (LLM constrained-label or keyword fallback) | PASS (`test_main_node`) |
| TC-D06 | KB retrieval: `相続税法第N条` article query | top chunk is the cited article (exact-match boost) | PASS (`test_main_node`) |

---

## Test Execution
- Every `test_*` file contains real assertions (no stub-only files).
- Run the full suite with `pytest tests/`.

## Test Execution Summary
- Execution date: (per CI run)
- Full suite: unit + proof-of-boundary + end-to-end `/invoke`, all passing under the
  released AgentCore SDK; caller-data validation matrices (non-finite numerics,
  malformed dates) and output-schema grid scans included.
