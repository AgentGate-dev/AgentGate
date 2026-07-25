"""MCP surface (PRD SS10 Slice 8, D44).

verify_action wraps decide() in-process under the SAME envelope and fail-closed
contract as the HTTP boundary: the same VerifyRequest validation (bounds,
extra="forbid", po rejected), a Decision dict ALWAYS returned — never an
exception surfaced to the MCP client — and the same boundary stamping. Money
must arrive as JSON strings: the MCP transport parses JSON before AgentGate
sees it, so a numeric amount is an already-lossy float and is rejected by the
D1 validator into a fail-closed escalate with an instructive message.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

from agentgate.mcp.server import mcp, verify_action

DEFAULT_RAW_TEXT = "Invoice INV-001 from Acme Corp. Total Due: $1,240.00"


def invoice_payload(**overrides) -> dict:
    payload = {
        "invoice_number": "INV-001",
        "vendor": "Acme Corp",
        "date": "2026-01-15",
        "currency": "USD",
        "line_items": [
            {
                "description": "Widget",
                "quantity": "1",
                "unit_price": {"value": "1240.00", "currency": "USD"},
                "amount": {"value": "1240.00", "currency": "USD"},
                "kind": "charge",
            }
        ],
        "tax_lines": [],
        "total": {"value": "1240.00", "currency": "USD"},
    }
    payload.update(overrides)
    return payload


def action_payload(**overrides) -> dict:
    payload = {
        "action_type": "approve_payment",
        "invoice_number": "INV-001",
        "amount": {"value": "1240.00", "currency": "USD"},
        "vendor": "Acme Corp",
        "adjustments": [],
        "agent_rationale": "Totals match.",
    }
    payload.update(overrides)
    return payload


def source_payload(**overrides) -> dict:
    source = {"invoice": invoice_payload(), "raw_text": DEFAULT_RAW_TEXT}
    source.update(overrides)
    return source


def assert_boundary_fields(decision: dict) -> None:
    uuid.UUID(decision["trace_id"])
    assert isinstance(decision["latency_ms"], int)
    assert decision["latency_ms"] >= 0
    assert datetime.fromisoformat(decision["timestamp"]).tzinfo is not None


def test_clean_request_returns_allow_decision():
    decision = verify_action(action_payload(), source_payload())
    assert decision["decision"] == "allow"
    assert decision["score"] == "1.00"  # Decimal stays a JSON string (D35/D44)
    assert len(decision["checks"]) == 7
    assert all(c["passed"] for c in decision["checks"])
    assert_boundary_fields(decision)


def test_tampered_amount_blocks_with_machine_readable_reason():
    action = action_payload(amount={"value": "12400.00", "currency": "USD"})
    decision = verify_action(action, source_payload())
    assert decision["decision"] == "block"
    (reason,) = decision["reasons"]
    assert reason["check"] == "action_amount_matches_total"
    assert reason["block_type"] == "agent_fixable"
    assert reason["expected"]["value"] == "1240.00"


def test_garbage_input_fails_closed_never_raises():
    decision = verify_action({"nonsense": True}, {})
    assert decision["decision"] == "escalate"
    assert decision["score"] is None
    assert decision["checks"] == []
    assert all(r["check"] == "fail_closed" for r in decision["reasons"])
    assert_boundary_fields(decision)


def test_float_money_is_rejected_with_an_instructive_message():
    # The MCP transport already parsed the JSON, so 1240.0 arrives as a float —
    # rejecting it (rather than laundering it through str()) is the only
    # D1-honest behavior (D44).
    action = action_payload(amount={"value": 1240.0, "currency": "USD"})
    decision = verify_action(action, source_payload())
    assert decision["decision"] == "escalate"
    (reason,) = decision["reasons"]
    assert reason["check"] == "fail_closed"
    assert "never float" in reason["message"]


def test_structured_invoice_without_raw_text_fails_closed():
    decision = verify_action(action_payload(), {"invoice": invoice_payload()})
    assert decision["decision"] == "escalate"
    (reason,) = decision["reasons"]
    assert reason["check"] == "fail_closed"
    assert "raw_text" in reason["message"].lower()


def test_po_evidence_is_rejected_like_http():
    decision = verify_action(
        action_payload(), source_payload(po={"po_number": "PO-1"})
    )
    assert decision["decision"] == "escalate"
    (reason,) = decision["reasons"]
    assert reason["check"] == "fail_closed"
    assert "po" in reason["message"]


def test_unexpected_internal_error_fails_closed(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("agentgate.mcp.server.decide", boom)
    decision = verify_action(action_payload(), source_payload())
    assert decision["decision"] == "escalate"
    (reason,) = decision["reasons"]
    assert reason["check"] == "fail_closed"
    assert "kaboom" in reason["message"]


def test_verify_action_is_registered_as_an_mcp_tool():
    tools = asyncio.run(mcp.list_tools())
    assert "verify_action" in [t.name for t in tools]


def test_fetch_mode_verifies_against_the_system_of_record(monkeypatch, tmp_path):
    # Fetch mode over MCP (D45): the tool resolves the record itself; the
    # calling agent supplies only the identifier.
    import json as _json

    from agentgate.core.system_of_record import DirectorySourceOfRecord

    record = {
        "invoice": invoice_payload(),
        "raw_text": "Acme Corp — Invoice INV-001. Total due: $1,240.00 USD.",
    }
    (tmp_path / "r.json").write_text(_json.dumps(record), encoding="utf-8")
    monkeypatch.setattr(
        "agentgate.mcp.server._source_of_record", DirectorySourceOfRecord(tmp_path)
    )
    decision = verify_action(action_payload(), {"fetch": "INV-001"})
    assert decision["decision"] == "allow"
    assert decision["evidence_used"] == [
        "system_of_record:invoice:INV-001",
        "system_of_record:raw_text",
    ]
    assert_boundary_fields(decision)


def test_fetch_mode_not_found_fails_closed_over_mcp(monkeypatch, tmp_path):
    from agentgate.core.system_of_record import DirectorySourceOfRecord

    monkeypatch.setattr(
        "agentgate.mcp.server._source_of_record", DirectorySourceOfRecord(tmp_path)
    )
    decision = verify_action(action_payload(), {"fetch": "INV-404"})
    assert decision["decision"] == "escalate"
    (reason,) = decision["reasons"]
    assert reason["check"] == "fail_closed"
    assert "not found" in reason["message"]
