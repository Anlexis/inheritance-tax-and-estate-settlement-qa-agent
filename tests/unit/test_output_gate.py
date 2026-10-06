# FIN-C2-017 — Unit Tests: the module-level output gate
#
# The documented external-output schema: every monetary value in the response
# sits on the 1,000 grid. The gate enforces the invariant COMPLETELY — by FORM
# (comma-grouped, 5+-digit runs) and by CURRENCY CONTEXT (marker before or
# after, code or symbol incl. fullwidth, attached or separated by ANY
# whitespace run, signed or unsigned) — at every magnitude, with no
# exemptions. Structural tokens (dates, horizons, bare counts, embedded
# acronyms, JP scale-marker notation like 3000万円) stay byte-identical.
# Boundary tests run BOTH ways: every leak form snaps; every structural form
# is untouched.

import pytest

from src.nodes.post_process_node import (
    _BLOCKER_NOTICE,
    _enforce_precision,
    _security_gate_output,
)


class TestEnforcePrecisionSnapsEveryLeakForm:
    @pytest.mark.parametrize(
        ("leak", "expected"),
        [
            # form-based: comma-grouped at any magnitude
            ("残高 9,999 を確認", "残高 10,000 を確認"),
            ("累計 1,234,567 円規模", "累計 1,235,000 円規模"),
            # form-based: bare 5+-digit runs
            ("12345", "12,000"),
            ("評価額は 4800001 とする", "評価額は 4,800,000 とする"),
            # currency context, marker AFTER: bare short value
            ("9999 JPY", "10,000 JPY"),
            ("9999円", "10,000円"),
            ("999円", "1,000円"),
            ("9999 $", "10,000 $"),
            # currency context, marker BEFORE
            ("JPY 9999", "JPY 10,000"),
            ("¥9999", "¥10,000"),
            ("￥9999", "￥10,000"),
            ("USD 9999", "USD 10,000"),
            # signed values, sign preserved
            ("JPY -9999", "JPY -10,000"),
            ("JPY +9999", "JPY +10,000"),
            ("-9999円", "-10,000円"),
            # arbitrary whitespace delimiters (tabs, newlines, runs)
            ("JPY  9999", "JPY  10,000"),
            ("JPY\t9999", "JPY\t10,000"),
            ("JPY\n9999", "JPY\n10,000"),
            ("9999\t\tJPY", "10,000\t\tJPY"),
        ],
    )
    def test_off_grid_forms_snap(self, leak, expected):
        sanitised, redactions = _enforce_precision(leak)
        assert sanitised == expected
        assert redactions == 1

    @pytest.mark.parametrize(
        "clean",
        [
            "42,000,000円",  # on-grid comma-grouped
            "3,000万円 + 600万円",  # statutory formula prose (on-grid / scale-marker)
            "3000万円",  # 4-digit + scale marker: structural JP notation
            "基礎控除額を確認（3000万円+600万円×法定相続人の数）（相続税法第15条）",
            "申告期限の目安: 2026-11-15（相続税法第27条）",  # ISO date + article number
            "残り12日です",  # bare count
            "10か月以内",  # statutory horizon
            "90d",  # lowercase horizon token
            "STAR 2026",  # embedded acronym + year (word-boundary guard)
            "year 2026",  # year without currency adjacency
            "3人",  # heir count
            "（金額は1,000円単位表示）",  # the schema note itself
        ],
    )
    def test_structural_tokens_stay_byte_identical(self, clean):
        sanitised, redactions = _enforce_precision(clean)
        assert sanitised == clean
        assert redactions == 0

    def test_on_grid_currency_values_untouched(self):
        sanitised, redactions = _enforce_precision("JPY 1000 / 2,000円 / ¥3000")
        assert sanitised == "JPY 1000 / 2,000円 / ¥3000"
        assert redactions == 0


class TestSecurityGateOutput:
    def test_credential_pattern_withholds_the_answer(self):
        result = _security_gate_output("手順: sk-ABCDEFGHIJKLMNOPQRSTUVWX0123 を使用", {"session_id": "s"})
        assert result == _BLOCKER_NOTICE
        assert "sk-" not in result

    @pytest.mark.parametrize(
        "leak",
        [
            "eyJhbGciOiJIUzI1NiJ9.payload.sig",
            "AKIAABCDEFGHIJKLMNOP",
            "Bearer abcdefghijklmnopqrstuvwxyz",
            'api_key = "supersecretvalue"',
        ],
    )
    def test_all_credential_families_withhold(self, leak):
        assert _security_gate_output(f"次を設定: {leak}", {}) == _BLOCKER_NOTICE

    def test_verbatim_raw_input_embedding_is_redacted(self):
        """The caller's raw question must never be embedded verbatim."""
        raw = "遺産が312,456,789円ある場合の相続税は"
        out = _security_gate_output(f"ご質問「{raw}」について:\n一般的な手続きをご案内します。", {"user_input": raw})
        assert raw not in out
        assert "[REDACTED]" in out

    def test_precision_layer_runs_after_redaction(self):
        out = _security_gate_output("試算 9,999 円", {"user_input": "short"})
        assert "10,000" in out

    def test_clean_text_passes_unchanged(self):
        text = "必要書類（戸籍謄本・遺産分割協議書等）を準備し所轄税務署へ申告\n\n申告期限の目安: 2026-11-15"
        assert _security_gate_output(text, {"user_input": "必要書類は？"}) == text
