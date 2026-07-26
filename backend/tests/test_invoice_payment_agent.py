"""Tests for the standalone invoice payment agent (HTTP client to AgentGate)."""

from __future__ import annotations

from pathlib import Path

from agentgate.agent.invoice_payment_agent import InvoicePaymentAgent

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "sample_invoices"


def _scripted_llm(*outputs: str):
    state = {"i": 0}

    def _call(_prompt: str) -> str:
        i = state["i"]
        state["i"] = i + 1
        return outputs[min(i, len(outputs) - 1)]

    return _call


class FakeGate:
    """Minimal in-process gate stub matching the HTTP contract."""

    def __init__(self) -> None:
        self.verify_calls = 0
        self.pay_calls = 0

    def verify(self, _payload: dict) -> dict:
        self.verify_calls += 1
        if self.verify_calls == 1:
            return {
                "decision": "block",
                "score": "0.50",
                "reasons": [
                    {
                        "check": "action_amount_matches_total",
                        "message": "amount does not match invoice total",
                        "block_type": "agent_fixable",
                        "field_to_change": "proposed_action.amount",
                        "expected": {"value": "1240.00", "currency": "USD"},
                        "received": {"value": "12400.00", "currency": "USD"},
                    }
                ],
                "checks": [],
            }
        return {"decision": "allow", "score": "1.00", "reasons": [], "checks": []}

    def pay(self, _payload: dict) -> dict:
        self.pay_calls += 1
        return {
            "decision": {"decision": "allow", "score": "1.00"},
            "execution": {
                "status": "paid",
                "payment": {"payment_id": "pay_test_agentdemo", "status": "succeeded"},
            },
        }


def test_agent_verifies_then_pays_via_gate(monkeypatch):
    gate = FakeGate()
    lines: list[str] = []

    def fake_post(url: str, payload: dict) -> dict:
        if url.endswith("/verify"):
            return gate.verify(payload)
        if url.endswith("/orchestrator/pay"):
            return gate.pay(payload)
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr("agentgate.agent.invoice_payment_agent._post_json", fake_post)

    llm = _scripted_llm(
        '{"action_type": "approve_payment", "invoice_number": "INV-001", '
        '"amount": {"value": "12400.00", "currency": "USD"}, "vendor": "Acme Corp"}',
        '{"value": "1240.00", "currency": "USD"}',
    )

    agent = InvoicePaymentAgent(
        gate_base_url="http://fake-gate:8000",
        llm_call=llm,
        out=lines.append,
    )
    result = agent.run((SAMPLES / "acme_good.txt").read_text())

    assert result["outcome"] == "paid"
    assert gate.verify_calls == 2
    assert gate.pay_calls == 1
    assert any("Gate ALLOW" in line for line in lines)
    assert any("payment_id: pay_test_agentdemo" in line for line in lines)
