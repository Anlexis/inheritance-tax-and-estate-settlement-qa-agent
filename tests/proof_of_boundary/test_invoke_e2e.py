# PB: End-to-end business behaviour through POST /invoke — src/api/server.py
#
# Proves the supported input contract produces REAL outcomes through the full
# graph (backbone + domain pipeline), not a stub baseline:
#   - a statutory filing-deadline calendar computed from the caller's
#     date_of_death (each urgency path: normal / within-30-days / passed)
#   - the statutory basic deduction computed from the caller's heirs_count
#   - a validation rejection for malformed caller data (fail CLOSED, no output)
#   - the documented external-output schema (every monetary token on the grid)
#   - the entry-point auth boundary (Bearer token -> VERIFIED_EXTERNAL)
#
# These tests run the REAL compiled agent through its real ASGI interface:
# every request crosses the entry-point auth, the trust/input gates, the
# domain pipeline, and the output gate.

import asyncio
import json
import re

import pytest

from src.api import server as server_module  # noqa: F401  (import = boot check)
from src.api.server import app

_TOKEN = "pb-invoke-e2e-token"

_INPUT_TEXT = "相続税の申告期限と必要な手続きを教えてください。"

# Pinned reference date so day counts are deterministic in every environment.
_AS_OF = "2026-08-20"


def _post_invoke(payload: dict, token: str | None = _TOKEN) -> tuple[int, dict]:
    """POST /invoke through the real ASGI app (optionally with a Bearer token)."""
    body = json.dumps(payload).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
    ]
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/invoke",
        "raw_path": b"/invoke",
        "root_path": "",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8000),
    }

    messages = []
    sent = {"body": b""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body":
            sent["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    parsed = json.loads(sent["body"].decode() or "{}")
    return start["status"], parsed


@pytest.fixture(autouse=True)
def token_configured(monkeypatch):
    """Deploy-shaped server environment: INVOKE_AUTH_TOKEN set, caller uses Bearer."""
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)


def _invoke(input_context: dict, input_text: str = _INPUT_TEXT) -> dict:
    status_code, body = _post_invoke(
        {"input": input_text, "session_id": "pb-invoke-e2e", "input_context": input_context}
    )
    assert status_code == 200, f"expected 200, got {status_code}: {body}"
    return body


class TestInvokeEndToEnd:
    def test_caller_data_produces_real_domain_outputs(self):
        """Caller-provided case data must yield the real deadline + deduction."""
        body = _invoke({"date_of_death": "2026-01-15", "as_of_date": _AS_OF, "heirs_count": 2})

        assert body["status"] == "success"
        output = body["output"]
        assert "2026-11-15" in output, output  # dod + 10 months (相続税法第27条)
        assert "42,000,000円" in output, output  # 30M + 6M × 2 (相続税法第15条)
        assert "法定相続人2人" in output
        assert "税理士" in output  # mandatory referral

    def test_baseline_without_caller_data(self):
        """No input_context → graceful baseline: guidance only, no calendar/figures."""
        body = _invoke({})

        assert body["status"] == "success"
        output = body["output"]
        assert output, "baseline answer must not be empty"
        assert "税理士" in output
        assert "基礎控除額の目安" not in output
        assert "申告期限の目安" not in output

    def test_deadline_warning_path(self):
        """A deadline within 30 days renders the countdown warning."""
        body = _invoke({"date_of_death": "2025-11-01", "as_of_date": _AS_OF})

        assert body["status"] == "success"
        output = body["output"]
        assert "【期限警告】" in output, output
        assert "残り12日" in output  # 2026-09-01 minus 2026-08-20
        assert "【期限注意】" not in output

    def test_deadline_passed_path(self):
        """A missed statutory deadline renders the overdue notice."""
        body = _invoke({"date_of_death": "2025-01-01", "as_of_date": _AS_OF})

        assert body["status"] == "success"
        output = body["output"]
        assert "【期限注意】" in output, output
        assert "を過ぎています" in output
        assert "【期限警告】" not in output

    def test_liability_calculation_is_scoped_out(self):
        """An individualized tax-liability request is refused with the scope-out."""
        body = _invoke(
            {"date_of_death": "2026-01-15", "heirs_count": 3},
            input_text="私の相続税額はいくらになりますか。計算してください。",
        )

        assert body["status"] == "success"
        output = body["output"]
        assert "個別の相続税額の計算はこのサービスでは行えません" in output
        assert "税理士" in output
        # the scope-out path renders no computed figures
        assert "基礎控除額の目安" not in output
        assert "48,000,000" not in output

    def test_invalid_caller_dates_are_rejected(self):
        """A malformed date_of_death must produce a validation error, not an answer."""
        body = _invoke({"date_of_death": "01/15/2026"})

        assert body["status"] == "success"
        assert body.get("output"), body
        assert (
            "could not be accepted" in body["output"]
            or "No question was received" in body["output"]
            or "too long" in body["output"]
        )

    @pytest.mark.parametrize(
        "bad_heirs",
        ["NaN", "Infinity", "-Infinity", float("nan"), float("inf"), 0, 51, 2.5, True],
        ids=["str-nan", "str-inf", "str-neginf", "raw-nan", "raw-inf", "zero", "over-max", "fraction", "bool"],
    )
    def test_non_finite_or_out_of_range_heirs_fail_closed(self, bad_heirs):
        """A NaN/Infinity/out-of-range heirs_count must ERROR with no answer —
        never a 'success with a wrong deduction' fail-open (raw floats also cover
        Python json's bare-NaN extension reaching the request body)."""
        body = _invoke({"date_of_death": "2026-01-15", "heirs_count": bad_heirs})

        assert body["status"] == "success", body
        assert body.get("output"), body
        assert (
            "could not be accepted" in body["output"]
            or "No question was received" in body["output"]
            or "too long" in body["output"]
        )

    def test_external_answer_carries_only_grid_values(self):
        """Approved external schema: every monetary-form figure sits on the 1,000 grid."""
        body = _invoke({"date_of_death": "2026-01-15", "as_of_date": _AS_OF, "heirs_count": 7})

        for token in re.findall(r"-?\d{1,3}(?:,\d{3})+|-?\d{5,}", body["output"]):
            value = int(token.replace(",", ""))
            assert value % 1_000 == 0, f"off-grid value leaked: {token}"

    def test_unauthenticated_caller_is_refused(self):
        """Without the Bearer token the adapter returns 401 before the graph runs."""
        status_code, body = _post_invoke({"input": _INPUT_TEXT}, token=None)
        assert status_code == 401
        assert "output" not in body

    def test_wrong_token_is_refused(self):
        status_code, _ = _post_invoke({"input": _INPUT_TEXT}, token="wrong-token")
        assert status_code == 401

    def test_oversized_input_context_is_refused(self):
        """The adapter-level size cap rejects oversized payloads with 413."""
        status_code, _ = _post_invoke({"input": _INPUT_TEXT, "input_context": {"padding": "x" * 300_000}})
        assert status_code == 413
