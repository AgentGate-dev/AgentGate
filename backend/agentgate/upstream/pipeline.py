"""End-to-end upstream pipeline: parse → propose → verify (gate only)."""

from __future__ import annotations

from typing import Any

from agentgate.mcp.server import verify_action

from .invoice_text import ParsedInvoice, parse_invoice_text, parsed_invoice_to_wire


def default_proposed_action_from_wire(invoice: dict[str, Any]) -> dict[str, Any]:
    """Build a payment proposal from a wire-format invoice dict."""
    from agentgate.core.schemas import Invoice

    model = Invoice.model_validate(invoice)
    return {
        "action_type": "approve_payment",
        "invoice_number": model.invoice_number,
        "amount": {"value": str(model.total.value), "currency": model.total.currency},
        "vendor": model.vendor,
        "adjustments": [],
        "agent_rationale": "Agent proposes payment matching invoice total and vendor.",
    }


def default_proposed_action(parsed: ParsedInvoice) -> dict[str, Any]:
    """Deterministic agent proposal — pay invoice total to invoice vendor."""
    return default_proposed_action_from_wire(parsed_invoice_to_wire(parsed))


def process_invoice(raw_text: str) -> dict[str, Any]:
    """Parse invoice text, propose payment, and call the gate (``verify_action``).

    Returns upstream artifacts plus the gate Decision. The Decision is the only
    authoritative ALLOW / BLOCK / ESCALATE outcome.
    """
    parsed = parse_invoice_text(raw_text)
    proposed_action = default_proposed_action(parsed)
    source = {
        "invoice": parsed_invoice_to_wire(parsed),
        "raw_text": parsed.raw_text,
    }
    decision = verify_action(proposed_action, source)
    return {
        "parsed_invoice": parsed_invoice_to_wire(parsed),
        "proposed_action": proposed_action,
        "source_mode": "caller",
        "decision": decision,
    }


def process_fetch(invoice_number: str, proposed_action: dict[str, Any]) -> dict[str, Any]:
    """Fetch-mode pipeline: agent proposes against a system-of-record identifier."""
    decision = verify_action(proposed_action, {"fetch": invoice_number.strip()})
    return {
        "fetch": invoice_number.strip(),
        "proposed_action": proposed_action,
        "source_mode": "system_of_record",
        "decision": decision,
    }
