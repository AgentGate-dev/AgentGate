"""Tests for source grounding and LLM-output JSON recovery (D19/D21/D22).

Two surviving surfaces from the original walking skeleton (its extract-then-
ground pipeline was removed as dead code — D19 named it disposable, and no
production caller existed): ``is_grounded``/``money_tokens`` (deterministic
token-level Decimal matching; the verification path's grounding primitive) and
``parse_llm_json`` (JSON recovery from messy model output; consumed by the
demo agent's proposal step).

Mutation checks (a plausible wrong impl MUST redden >=1 test):
  * substring grounding instead of token-level -> the anti-substring tests redden
  * grounding hard-coded True -> anti-substring / not-grounded assertions redden
  * float money instead of Decimal-from-string -> test_money_rejects_float reddens
  * parse_llm_json crashing (or returning junk) on truncation -> the
    fail-closed ExtractionError test reddens
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from agentgate.core.grounding import is_grounded, money_tokens
from agentgate.core.llm_json import ExtractionError, parse_llm_json
from agentgate.core.schemas import Money
from tests.conftest import load_sample

GOOD = load_sample("acme_good.txt")  # realistic invoice; Total Due $1,240.00


# --- grounding: token-level Decimal matching (D21) -----------------------------


def test_grounded_in_realistic_invoice_text():
    # The real total grounds against the sample's grouped "$1,240.00".
    assert is_grounded(Decimal("1240.00"), GOOD)


def test_not_grounded_when_value_absent():
    assert not is_grounded(Decimal("9999.99"), GOOD)


def test_decimal_lossless_across_formats():
    assert is_grounded(Decimal("1240"), "Total: $1,240.00")
    assert is_grounded(Decimal("1240.00"), "Total: 1240")


def test_anti_substring_guard():
    # 1240 hides inside INV-31240, 12/40, and $11,240.00 — none is the value (D21).
    raw = "Reference INV-31240 dated 12/40 for prior period. Amount billed: $11,240.00."
    assert not is_grounded(Decimal("1240.00"), raw)


def test_money_tokens_are_token_level():
    raw = "INV-31240 on 12/40 billed $11,240.00 and also 1240.00 exactly."
    tokens = money_tokens(raw)
    assert Decimal("11240.00") in tokens
    assert Decimal("1240.00") in tokens  # the standalone one IS present
    # The spurious 1240 that a substring scan would find is NOT a token here:
    assert Decimal("31240") not in tokens


def test_is_grounded_matches_across_formats():
    assert is_grounded(Decimal("1240"), "Total: $1,240.00")
    assert is_grounded(Decimal("1240.00"), "Total: 1240")
    assert not is_grounded(Decimal("1240"), "Order INV-31240 total 12/40")


def test_decimal_comma_is_not_a_left_partial_match():
    # A European decimal-comma amount 1240,50 must NOT tokenize to 1240: the
    # digits before the comma are a fractional part, not a standalone total.
    # This is the anti-substring guard (D21) on the RIGHT boundary — the mirror
    # of INV-31240 on the left. A false 1240 token would let a misread total
    # sail past the decisive total-grounding gate (D27).
    assert Decimal("1240") not in money_tokens("Gesamtbetrag: 1240,50 EUR")
    assert not is_grounded(Decimal("1240"), "Rechnung Gesamt: 1240,50 EUR")
    # Grouped thousands with trailing extra digits must not emit a truncated token.
    assert Decimal("1") not in money_tokens("value 1,2400 here")


def test_scientific_notation_plus_is_not_a_partial_match():
    # '+' must terminate a token on both sides, so 1E+3 never yields a bare 1 or 3.
    assert Decimal("1") not in money_tokens("coefficient 1E+3 applied")
    assert Decimal("3") not in money_tokens("coefficient 1E+3 applied")


# --- parse_llm_json: messy model output still yields an object (D22) -----------


def test_messy_output_still_parses():
    raw = (
        "Sure! Based on the invoice, here is the JSON you asked for:\n\n"
        "```json\n"
        '{\n    "total_value": "1240.00",\n    "currency": "USD",\n}\n'  # trailing comma
        "```\n\n"
        "Let me know if you need anything else."
    )
    data = parse_llm_json(raw)
    assert data == {"total_value": "1240.00", "currency": "USD"}


def test_json_numbers_decode_to_exact_decimals():
    # parse_float=Decimal (D1): a bare 1240.00 must not become a float.
    data = parse_llm_json('{"value": 1240.00}')
    assert data["value"] == Decimal("1240.00")
    assert not isinstance(data["value"], float)


def test_truncated_output_fails_closed():
    with pytest.raises(ExtractionError):
        parse_llm_json('{"total_value": "1240.00", "curren')  # no closing brace


def test_no_json_object_fails_closed():
    with pytest.raises(ExtractionError):
        parse_llm_json("I could not find a total in that document.")


def test_invalid_json_between_braces_fails_closed():
    # Braces present but the content is not JSON — must raise, never return a
    # partial/empty object (an empty dict would silently drop every field).
    with pytest.raises(ExtractionError):
        parse_llm_json('{"total_value": 12,40, "currency": USD}')


def test_non_string_input_fails_closed():
    # bytes / a provider content-parts list must fail closed, not crash on .find
    # with an untyped AttributeError the caller's ExtractionError catch would miss.
    for bad in (b'{"a": 1}', [{"type": "text", "text": "{}"}], 123):
        with pytest.raises(ExtractionError):
            parse_llm_json(bad)


def test_valid_json_string_content_is_not_corrupted():
    # A comma before ']'/'}' INSIDE a string literal must survive: valid JSON is
    # parsed verbatim, never rewritten by the trailing-comma repair.
    data = parse_llm_json('{"vendor": "Acme, ] Inc", "rationale": "checked [a, b,]"}')
    assert data["vendor"] == "Acme, ] Inc"
    assert data["rationale"] == "checked [a, b,]"


# --- Money hard rule: Decimal-from-string, never float (D1) --------------------


def test_money_rejects_float():
    with pytest.raises(ValueError):
        Money(value=1240.00, currency="USD")  # float must be refused


def test_money_parses_string_and_int():
    assert Money(value="1,240.00".replace(",", ""), currency="USD").value == Decimal("1240.00")
    assert Money(value=1240, currency="USD").value == Decimal("1240")


def test_money_rejects_nan_and_infinity():
    # Non-finite values would raise InvalidOperation at a downstream comparison,
    # escaping an `except ValidationError` fail-closed catch (D34).
    for bad in ("NaN", "sNaN", "Infinity", "-Infinity"):
        with pytest.raises(ValueError):
            Money(value=bad, currency="USD")


def test_numeric_cap_applies_to_integers_and_decimals():
    # D34's 50-char cap must hold on the JSON-int and JSON-number(Decimal) paths,
    # not only the string path — otherwise a multi-thousand-digit value rides into
    # check details, the Decision echo, and traces.
    from agentgate.core.schemas import MAX_NUMERIC_CHARS

    big_int = 10 ** (MAX_NUMERIC_CHARS + 5)  # 56 digits
    with pytest.raises(ValueError):
        Money(value=big_int, currency="USD")
    with pytest.raises(ValueError):
        Money(value=Decimal("1" * (MAX_NUMERIC_CHARS + 5)), currency="USD")


def test_unambiguous_european_formats_tokenize_to_exact_values():
    # A correctly-parsed European invoice total (1240.50) must ground against its
    # own document text — otherwise every European invoice false-escalates
    # total_not_grounded (D59). Only unambiguous forms are widened: both
    # separators present (last one is the decimal) or comma + exactly 1-2
    # trailing digits.
    assert is_grounded(Decimal("1240.50"), "Gesamtbetrag: 1.240,50 EUR")
    assert is_grounded(Decimal("1240.50"), "Amount Due: 1240,50")
    assert is_grounded(Decimal("1234567.89"), "Total: 1.234.567,89")
    # The old anti-partial guarantee holds: the bare left half never grounds.
    assert Decimal("1240") not in money_tokens("Total: 1.240,50")


def test_indian_grouping_tokenizes_to_exact_value():
    assert is_grounded(Decimal("124000.00"), "Total Amount: ₹1,24,000.00")
    assert is_grounded(Decimal("124000"), "Total Amount: 1,24,000")
    tokens = money_tokens("Total Amount: 1,24,000.00")
    assert Decimal("1") not in tokens
    assert Decimal("24") not in tokens


def test_nbsp_grouping_tokenizes_to_exact_value():
    # NBSP between digit groups is a deliberate print artifact, never two
    # separate numbers (a regular space could be, and stays a boundary).
    assert is_grounded(Decimal("1240.50"), "Total: 1 240,50")
    assert not is_grounded(Decimal("1240"), "qty 1 240 units ordered")


def test_bare_dot_thousands_stays_conservative():
    # "1.240" alone is ambiguous (US 1.240 vs European 1240): never guessed into
    # 1240 - the fail-closed direction is an escalate on the total gate, not a
    # false grounding.
    assert not is_grounded(Decimal("1240"), "Total: 1.240")
    assert is_grounded(Decimal("1.24"), "Total: 1.240")
