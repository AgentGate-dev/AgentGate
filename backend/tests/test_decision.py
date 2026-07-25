"""Tests for the deterministic checks and the ALLOW/BLOCK/ESCALATE decision
(DECISIONS D3/D8/D12/D13/D15). Pure functions, Decimal money.

Gate: a tampered amount returns BLOCK with a typed payload
{check, expected, received, field_to_change}.

Mutation checks (a plausible wrong impl MUST redden >=1 test):
  * amount-misread routed to ESCALATE instead of BLOCK -> test_tampered_amount_blocks reddens
  * block_type hard-coded -> test_tampered_amount_blocks / test_source_invalid redden
  * arithmetic formula wrong (ignores discounts/shipping/tax) -> test_arithmetic_* reddens
  * precedence ESCALATE-before-BLOCK -> test_precedence_block_over_escalate reddens
  * vendor entity-normalized (treats 'LLC' as equal) -> test_vendor_entity_diff reddens
"""

from __future__ import annotations

from decimal import Decimal

from agentgate.core.decision import decide
from agentgate.core.duplicate_store import DuplicateStore
from agentgate.core.schemas import (
    BlockType,
    DecisionType,
    Invoice,
    LineItem,
    LineItemKind,
    Money,
    ProposedAction,
    TaxLine,
)


def m(value: str, currency: str = "USD") -> Money:
    return Money(value=value, currency=currency)


def make_invoice(
    *,
    total: str = "1240.00",
    currency: str = "USD",
    vendor: str = "Acme Corp",
    line_items=None,
    tax_lines=None,
) -> Invoice:
    """A consistent invoice: charges 200 + 1000 + shipping 40 == total 1240.00."""
    if line_items is None:
        line_items = [
            LineItem(description="Widget A", quantity=2, unit_price=m("100.00"), amount=m("200.00"), kind=LineItemKind.charge),
            LineItem(description="Widget B", quantity=5, unit_price=m("200.00"), amount=m("1000.00"), kind=LineItemKind.charge),
            LineItem(description="Shipping", quantity=1, unit_price=m("40.00"), amount=m("40.00"), kind=LineItemKind.shipping),
        ]
    return Invoice(
        invoice_number="INV-001", vendor=vendor, date="2026-01-15", currency=currency,
        line_items=line_items, subtotal=m("1240.00", currency),
        tax_lines=tax_lines or [], total=Money(value=total, currency=currency),
    )


def make_action(*, amount: str = "1240.00", currency: str = "USD", vendor: str = "Acme Corp", adjustments=None) -> ProposedAction:
    return ProposedAction(
        action_type="approve_payment", invoice_number="INV-001",
        amount=Money(value=amount, currency=currency), vendor=vendor,
        adjustments=adjustments or [],
    )


def test_negative_amount_is_out_of_scope_not_allowed():
    # A self-consistent NEGATIVE invoice (a credit note) is out of v1 scope and
    # must fail closed at the schema, never sail to ALLOW as a normal payment.
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_invoice(
            total="-1000.00",
            line_items=[
                LineItem(description="Credit", quantity=1, unit_price=m("-1000.00"),
                         amount=m("-1000.00"), kind=LineItemKind.charge),
            ],
        )
    with pytest.raises(ValidationError):
        make_action(amount="-1000.00")


def test_oversized_amount_fails_closed_not_overflow():
    # A magnitude beyond the ceiling must be rejected at the schema, never reach
    # the verifier's Decimal arithmetic (where a huge exponent overflows/crashes).
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_action(amount="1E+1000000")
    with pytest.raises(ValidationError):
        make_invoice(total="1000000000000000000000")  # 1e21, over the 1e18 ceiling


# --- GATE: tampered amount -> BLOCK with typed payload ------------------------
def test_tampered_amount_blocks():
    # invoice total 1240.00, agent proposes 12400.00 (a decimal slip), no adjustment.
    decision = decide(make_invoice(), make_action(amount="12400.00"))
    assert decision.decision is DecisionType.block
    assert len(decision.reasons) == 1
    r = decision.reasons[0]
    assert r.check == "action_amount_matches_total"
    assert r.expected == m("1240.00")          # the invoice total
    assert r.received == m("12400.00")          # the proposed amount
    assert r.field_to_change == "proposed_action.amount"
    assert r.block_type is BlockType.agent_fixable
    assert "1240.00" in r.message               # tells the agent the fix


# --- ALLOW: everything consistent --------------------------------------------
def test_all_consistent_allows():
    decision = decide(make_invoice(), make_action(amount="1240.00"))
    assert decision.decision is DecisionType.allow
    assert decision.reasons == []
    assert decision.score == Decimal("1.00")
    assert all(c.passed for c in decision.checks)


# --- amount != total WITH a declared adjustment -> ESCALATE (not BLOCK) -------
def test_amount_diff_with_declared_adjustment_escalates():
    action = make_action(amount="1116.00", adjustments=[{"type": "withholding", "value": "124.00"}])
    decision = decide(make_invoice(), action)
    assert decision.decision is DecisionType.escalate
    assert decision.reasons[0].check == "action_amount_matches_total"
    assert decision.reasons[0].block_type is None  # not a block


# --- currency mismatch -> ESCALATE (no FX) -----------------------------------
def test_currency_mismatch_escalates():
    decision = decide(make_invoice(currency="USD"), make_action(amount="1240.00", currency="EUR"))
    assert decision.decision is DecisionType.escalate
    assert any(r.check == "currency_match" for r in decision.reasons)


# --- structural arithmetic fail -> ESCALATE (source_invalid) -----------------
def test_source_invalid_arithmetic_escalates():
    # line items sum to 1240 but total claims 1300 -> source is internally broken.
    decision = decide(make_invoice(total="1300.00"), make_action(amount="1300.00"))
    assert decision.decision is DecisionType.escalate
    r = next(r for r in decision.reasons if r.check == "structural_arithmetic")
    assert r.block_type is BlockType.source_invalid


def test_arithmetic_handles_discount_shipping_and_tax():
    # charges 1000 + shipping 40 − discount 100 + tax(5)+tax(7) = 952 == total.
    items = [
        LineItem(description="Item", quantity=1, unit_price=m("1000.00"), amount=m("1000.00"), kind=LineItemKind.charge),
        LineItem(description="Ship", quantity=1, unit_price=m("40.00"), amount=m("40.00"), kind=LineItemKind.shipping),
        LineItem(description="Promo", quantity=1, unit_price=m("100.00"), amount=m("100.00"), kind=LineItemKind.discount),
    ]
    taxes = [TaxLine(rate="0.05", amount=m("5.00")), TaxLine(rate="0.07", amount=m("7.00"))]
    inv = make_invoice(total="952.00", line_items=items, tax_lines=taxes)
    decision = decide(inv, make_action(amount="952.00"))
    assert decision.decision is DecisionType.allow


def test_no_line_items_escalates_as_unverifiable():
    # A total-only invoice (no line items, no tax lines) is UNVERIFIABLE, not
    # internally inconsistent: escalate with no block_type, never source_invalid.
    decision = decide(make_invoice(total="199.00", line_items=[]), make_action(amount="199.00"))
    assert decision.decision is DecisionType.escalate
    r = next(r for r in decision.reasons if r.check == "structural_arithmetic")
    assert r.block_type is None
    assert "no line items" in r.message


def test_arithmetic_tolerance_one_cent():
    # sum 1240.00 vs total 1240.01 is within the $0.01 vendor-rounding tolerance.
    decision = decide(make_invoice(total="1240.01"), make_action(amount="1240.01"))
    assert next(c for c in decision.checks if c.name == "structural_arithmetic").passed


# --- vendor: cosmetic normalize passes; entity diff escalates (D8) -----------
def test_vendor_cosmetic_normalization_passes():
    decision = decide(make_invoice(vendor="Acme Corp"), make_action(amount="1240.00", vendor="  acme corp.  "))
    assert decision.decision is DecisionType.allow


def test_vendor_entity_diff_escalates():
    decision = decide(make_invoice(vendor="Acme Corp"), make_action(amount="1240.00", vendor="Acme Corp LLC"))
    assert decision.decision is DecisionType.escalate
    assert any(r.check == "vendor_match" for r in decision.reasons)


# --- duplicate -> ESCALATE (via SQLite store) --------------------------------
def test_duplicate_escalates_with_sqlite_store():
    store = DuplicateStore(":memory:")
    store.mark_approved("INV-001")
    decision = decide(make_invoice(), make_action(amount="1240.00"), is_duplicate=store.is_approved("INV-001"))
    assert decision.decision is DecisionType.escalate
    assert any(r.check == "duplicate_check" for r in decision.reasons)
    store.close()


def test_sqlite_store_roundtrip():
    store = DuplicateStore(":memory:")
    assert store.is_approved("INV-001") is False
    store.mark_approved("INV-001")
    assert store.is_approved("INV-001") is True
    store.close()


# --- precedence BLOCK > ESCALATE ---------------------------------------------
def test_precedence_block_over_escalate():
    # tampered amount (BLOCK) AND currency mismatch (ESCALATE) -> BLOCK wins.
    decision = decide(make_invoice(), make_action(amount="12400.00", currency="EUR"))
    assert decision.decision is DecisionType.block


# --- score reflects soft checks only -----------------------------------------
def test_score_drops_when_soft_check_fails():
    # vendor (soft) fails -> 1 of 2 soft checks pass -> score 0.50.
    decision = decide(make_invoice(vendor="Acme Corp"), make_action(amount="1240.00", vendor="Other Inc"))
    assert decision.score == Decimal("0.50")


def test_tax_line_rate_is_optional():
    # Many real documents state a tax amount with no rate; fabricating one would
    # be a lie. No check consumes rate - only tax amounts feed arithmetic.
    items = [
        LineItem(description="Item", quantity=1, unit_price=m("100.00"), amount=m("100.00"), kind=LineItemKind.charge),
    ]
    taxes = [TaxLine(amount=m("8.00"))]
    inv = make_invoice(total="108.00", line_items=items, tax_lines=taxes)
    decision = decide(inv, make_action(amount="108.00"))
    assert decision.decision is DecisionType.allow
    assert taxes[0].rate is None
