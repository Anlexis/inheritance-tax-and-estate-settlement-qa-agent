r"""Regression: decimal fractions, domain identifiers, and block-crossing markers.

Three defects lived in the precision gate's grammar at once, and each one
rewrote text the response is supposed to quote verbatim:

1. DECIMAL FRACTION — the fraction of a decimal is a standalone 5+-digit run in
   its own right, and `.` is not an identifier character, so the form-based
   branch latched onto it: a 路線価 correction ratio "8.512345" rendered
   "8.512,000" and a 配偶者の税額軽減 rate "99.99999%" rendered "99.100,000%".
   In currency context the integer snapped while the fraction dangled
   ("JPY 1234.56" -> "JPY 1,000.56") — neither the true figure nor on the grid.
   Absorption is written `(?:\.\d+|(?!\.\d))`, NOT `(?:\.\d+)?`: the optional
   form lets the engine backtrack out of the fraction and re-match the integer
   whenever the text after the fraction fails the trailing guard, so
   "JPY 1234.56m" came back out as "JPY 1,000.56m".

2. IDENTIFIER COLLISION — any standalone 3-letter uppercase word read as a
   currency marker, so a 国税庁 citation "NTA-4152" rendered "NTA-4,000". The
   whole value of a statutory-guidance answer is that its citation is
   checkable. Only an ISO-4217 code may bind to a value it is ATTACHED to;
   a marker SEPARATED by whitespace stays unrestricted, so the gate still
   fails safe on the ambiguous case.

3. BLOCK-CROSSING DELIMITER — `\s*` between marker and value spans a blank
   line, so a 3-letter code ending one block bound to the number opening the
   next and renumbered a step of the procedure checklist.

Every case below is a form this template actually renders: ISO filing
deadlines, 相続税法 article numbers, day counts, the {:,d} 基礎控除 figure,
kb_citations, and the KB/checklist prose that carries ratios and percentages.
Both directions are pinned: every leak form still snaps, every identifier and
decimal survives byte-identical.
"""

import pytest

from src.nodes.post_process_node import _enforce_precision


class TestDecimalsAreNotRewritten:
    @pytest.mark.parametrize(
        "clean",
        [
            "路線価は 1.05 倍率で補正",  # short correction ratio
            "評価額の割合は 8.512345 です",  # 6-digit fraction — the original leak
            "適用割合は 99.99999% まで",  # 配偶者の税額軽減 rate, long fraction
            "控除率 0.123456 を参照",  # bare ratio after a word
            "JPY 1234.56m",  # decimal + letter suffix: the BACKTRACKING case
            "小数点以下 0.5 は切り捨て",
        ],
    )
    def test_ratios_and_percentages_stay_byte_identical(self, clean):
        sanitised, redactions = _enforce_precision(clean)
        assert sanitised == clean
        assert redactions == 0

    def test_off_grid_decimal_amount_snaps_as_one_number(self):
        """An off-grid amount in currency context still snaps — as ONE number.

        The bug was never "the gate touches decimals"; it was that it snapped
        the integer part and left the fraction dangling.
        """
        sanitised, redactions = _enforce_precision("JPY 1234.56")
        assert sanitised == "JPY 1,000"
        assert redactions == 1


class TestDomainIdentifiersSurvive:
    @pytest.mark.parametrize(
        "clean",
        [
            "申告期限の目安: 2026-11-15（相続税法第27条）",  # ISO deadline + article
            "【期限警告】申告期限（2026-11-15）まで残り21日です。",  # ISO date + day count
            "根拠: 相続税法第19条の2、相続税法第15条",  # article with a 枝番
            "根拠: nta-4152、NTA-4152、law-15",  # kb_citations, both cases
            "根拠: NTA-41520、sozoku_41520",  # citation IDs with a 5-digit part
            "参考: https://www.nta.go.jp/taxes/shiraberu/41520.htm",  # HTML source as citation_id
            "参考(KB): 相続税法第15条 / 国税庁タックスアンサー 4152",
        ],
    )
    def test_citations_and_dates_stay_byte_identical(self, clean):
        sanitised, redactions = _enforce_precision(clean)
        assert sanitised == clean
        assert redactions == 0

    def test_attached_iso_code_still_snaps(self):
        """Restricting the ATTACHED form to ISO-4217 must not exempt real currency."""
        assert _enforce_precision("JPY-9999") == ("JPY-10,000", 1)

    def test_separated_marker_stays_unrestricted(self):
        """A whitespace-SEPARATED 3-letter code is still a marker — fail safe."""
        assert _enforce_precision("SKF 6205") == ("SKF 6,000", 1)


class TestMarkerDoesNotCrossABlankLine:
    def test_numbered_checklist_step_is_not_renumbered(self):
        """A 3-letter code ending a block must not bind to the next block's number.

        `\\s*` spans the paragraph break, so the synthesised checklist's step 3
        was rewritten to step 0 — the gate renumbering a statutory procedure.
        """
        text = "…タックスアンサー PDF\n\n3. 相続税の申告書を所轄税務署へ提出します（相続税法第27条）"
        assert _enforce_precision(text) == (text, 0)

    def test_two_digit_step_is_not_renumbered(self):
        text = "…国税庁 NTA\n\n15. 遺産分割協議書を作成します"
        assert _enforce_precision(text) == (text, 0)

    def test_single_newline_delimiter_still_snaps(self):
        """One newline is still a delimiter — only a paragraph break is not."""
        assert _enforce_precision("JPY\n9999") == ("JPY\n10,000", 1)


class TestJapaneseWordBoundaryIsPreserved:
    """The identifier guards are an ASCII class and cannot replace `\\b`.

    万 / 円 / 条 / 日 / 人 are word characters, so without the `\\b` after the
    marker-then-value branch the checklist's own statutory formula matches as
    円-marker + "+600" and renders "（3000万円+1,000万円×法定相続人の数）" —
    a misquoted 相続税法第15条 in the answer's most-quoted line.
    """

    @pytest.mark.parametrize(
        "clean",
        [
            "基礎控除額を確認（3000万円+600万円×法定相続人の数）（相続税法第15条）",
            "基礎控除額の目安: 138,000,000円（3,000万円 + 600万円 × 法定相続人18人・相続税法第15条）（金額は1,000円単位表示）",
            "配偶者の税額軽減・各種控除の適用要件を確認（相続税法第19条の2 ほか）",
        ],
    )
    def test_statutory_formula_prose_stays_byte_identical(self, clean):
        sanitised, redactions = _enforce_precision(clean)
        assert sanitised == clean
        assert redactions == 0


class TestGridControlsStillHold:
    @pytest.mark.parametrize(
        ("text", "expected", "count"),
        [
            ("JPY 1,000", "JPY 1,000", 0),  # on-grid, byte-identical
            ("JPY 1,234", "JPY 1,000", 1),  # off-grid grouped
            ("試算 9,999 円", "試算 10,000 円", 1),
            ("評価額は 4800001 とする", "評価額は 4,800,000 とする", 1),
            ("9999円", "10,000円", 1),
        ],
    )
    def test_leak_forms_still_snap(self, text, expected, count):
        assert _enforce_precision(text) == (expected, count)
