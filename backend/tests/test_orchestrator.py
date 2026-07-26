"""Orchestrator execute layer — verify then pay on ALLOW.

Money-safety contract (PRD §5b, D50 ordering): the duplicate-store reserve
happens BEFORE the payment rail runs, so a duplicate can never race past the
check and an "aborted duplicate" status can never coexist with an executed
payment. Approval resolution is first-resolution-wins; pending approvals
expire; an execution failure after reserve keeps the invoice number burned.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentgate.core.duplicate_store import DuplicateStore
from agentgate.core.policy import DEFAULT_POLICY
from agentgate.main import create_app
from agentgate.orchestrator.payment import PaymentResult, TestPaymentProvider
from agentgate.orchestrator.service import Orchestrator
from agentgate.orchestrator.store import OrchestratorStore

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "sample_invoices"
NORTHWIND = (
    Path(__file__).resolve().parent.parent.parent
    / "frontend"
    / "public"
    / "invoices"
    / "northwind-inv-12500.txt"
)


def make_orchestrator(
    dup: DuplicateStore | None = None,
    provider=None,
    clock=None,
    approval_ttl_hours: int | None = None,
):
    dup = dup if dup is not None else DuplicateStore()
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    if approval_ttl_hours is not None:
        kwargs["approval_ttl_hours"] = approval_ttl_hours
    orch = Orchestrator(
        duplicate_store=dup,
        orchestrator_store=OrchestratorStore(),
        policy=DEFAULT_POLICY,
        source_of_record=None,
        payment_provider=provider or TestPaymentProvider(),
        **kwargs,
    )
    return orch, dup


class ReserveCheckingProvider:
    """Fails unless the invoice number is already reserved when the rail runs."""

    mode = "test"

    def __init__(self, dup: DuplicateStore) -> None:
        self._dup = dup
        self.executed: list[str] = []

    def execute(self, proposed_action) -> PaymentResult:
        number = str(proposed_action["invoice_number"])
        assert self._dup.is_approved(number), (
            "payment rail ran before the duplicate-store reserve (D50 ordering)"
        )
        self.executed.append(number)
        amount = proposed_action["amount"]
        return PaymentResult(
            payment_id=f"pay_test_{uuid.uuid4().hex[:16]}",
            status="succeeded",
            mode=self.mode,
            amount_value=str(amount["value"]),
            amount_currency=str(amount["currency"]),
            vendor=str(proposed_action["vendor"]),
            invoice_number=number,
        )


class ExplodingProvider:
    mode = "test"

    def execute(self, proposed_action) -> PaymentResult:
        raise RuntimeError("payment rail unavailable")


def test_execute_pays_on_allow():
    orch, dup = make_orchestrator()
    result = orch.execute_invoice((SAMPLES / "acme_good.txt").read_text())
    assert result["decision"]["decision"] == "allow"
    assert result["execution"]["status"] == "paid"
    assert result["execution"]["payment"]["payment_id"].startswith("pay_test_")
    assert dup.is_approved("INV-001")


def test_reserve_happens_before_payment_executes():
    dup = DuplicateStore()
    provider = ReserveCheckingProvider(dup)
    orch, _ = make_orchestrator(dup=dup, provider=provider)
    result = orch.execute_invoice((SAMPLES / "acme_good.txt").read_text())
    assert result["execution"]["status"] == "paid"
    assert provider.executed == ["INV-001"]


def test_execution_failure_after_reserve_burns_number_and_reports():
    # D50: unknown-outcome after reserve means a human investigates; the number
    # stays burned and the caller gets a typed status, never a crash.
    orch, dup = make_orchestrator(provider=ExplodingProvider())
    result = orch.execute_invoice((SAMPLES / "acme_good.txt").read_text())
    assert result["execution"]["status"] == "allowed_execution_failed"
    assert dup.is_approved("INV-001")


def test_execute_escalate_queues_approval():
    orch, _ = make_orchestrator()
    result = orch.execute_invoice(NORTHWIND.read_text())
    assert result["decision"]["decision"] == "escalate"
    assert result["execution"]["status"] == "pending_human_approval"
    approval_id = result["execution"]["approval_id"]

    paid = orch.approve_and_execute(approval_id, approved=True)
    assert paid["execution"]["status"] == "paid_after_human_approval"


def test_resolve_approval_is_first_resolution_wins():
    store = OrchestratorStore()
    approval_id = store.create_pending_approval(
        invoice_number="INV-9",
        proposed_action={"invoice_number": "INV-9"},
        source={},
        gate_trace_id=None,
    )
    assert store.resolve_approval(approval_id, approved=True) is True
    # The second resolution loses, whatever its verdict — never re-executed.
    assert store.resolve_approval(approval_id, approved=False) is False


def test_second_human_decision_is_refused():
    orch, _ = make_orchestrator()
    result = orch.execute_invoice(NORTHWIND.read_text())
    approval_id = result["execution"]["approval_id"]
    orch.approve_and_execute(approval_id, approved=True)
    with pytest.raises(ValueError, match="already"):
        orch.approve_and_execute(approval_id, approved=True)


def test_expired_approval_executes_nothing():
    frozen = [datetime.now(timezone.utc)]
    orch, dup = make_orchestrator(clock=lambda: frozen[0], approval_ttl_hours=72)
    result = orch.execute_invoice(NORTHWIND.read_text())
    approval_id = result["execution"]["approval_id"]

    frozen[0] = frozen[0] + timedelta(hours=73)
    expired = orch.approve_and_execute(approval_id, approved=True)
    assert expired["execution"]["status"] == "approval_expired"
    assert not dup.is_approved("INV-2026-0201")


def test_duplicate_abort_reports_no_payment():
    # Approving a pending escalation for an invoice that was meanwhile paid must
    # abort WITHOUT running the rail — and the response must not carry a
    # payment object claiming otherwise.
    orch, dup = make_orchestrator()
    first = orch.execute_invoice(NORTHWIND.read_text())
    approval_a = first["execution"]["approval_id"]
    paid = orch.approve_and_execute(approval_a, approved=True)
    assert paid["execution"]["status"] == "paid_after_human_approval"

    second = orch.execute_invoice(NORTHWIND.read_text())
    assert second["decision"]["decision"] == "escalate"
    approval_b = second["execution"]["approval_id"]
    aborted = orch.approve_and_execute(approval_b, approved=True)
    assert aborted["execution"]["status"] == "payment_aborted_duplicate"
    assert "payment" not in aborted["execution"]


def test_orchestrator_http_execute():
    app = create_app(cors_origins=["http://127.0.0.1:3000"])
    client = TestClient(app)
    text = (SAMPLES / "acme_good.txt").read_text()
    resp = client.post("/orchestrator/execute", json={"raw_text": text})
    assert resp.status_code == 200
    body = resp.json()
    assert body["execution"]["status"] == "paid"


def test_orchestrator_http_pay_agent_proposal():
    """External agents submit proposals — the gate re-verifies before paying."""
    from agentgate.upstream.invoice_text import parse_invoice_text, parsed_invoice_to_wire
    from agentgate.upstream.pipeline import default_proposed_action

    app = create_app(cors_origins=["http://127.0.0.1:3000"])
    client = TestClient(app)
    text = (SAMPLES / "acme_good.txt").read_text()
    parsed = parse_invoice_text(text)
    wire = parsed_invoice_to_wire(parsed)
    source = {"invoice": wire, "raw_text": parsed.raw_text}
    proposed = default_proposed_action(parsed)

    resp = client.post(
        "/orchestrator/pay",
        json={"proposed_action": proposed, "source": source},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"]["decision"] == "allow"
    assert body["execution"]["status"] == "paid"


def test_duplicate_blocks_second_payment():
    orch, _ = make_orchestrator()
    text = (SAMPLES / "acme_good.txt").read_text()
    first = orch.execute_invoice(text)
    assert first["execution"]["status"] == "paid"
    second = orch.execute_invoice(text)
    assert second["decision"]["decision"] == "escalate"
    assert any(r["check"] == "duplicate_check" for r in second["decision"]["reasons"])


def test_execute_with_llm_agent_uses_injected_proposal():
    """The LLM seam is injectable — verification runs on the model's proposal."""
    text = (SAMPLES / "acme_good.txt").read_text()

    def fake_llm(_prompt: str) -> str:
        return (
            '{"action_type": "approve_payment", "invoice_number": "INV-001", '
            '"amount": {"value": "1240.00", "currency": "USD"}, "vendor": "Acme Corp"}'
        )

    orch, dup = make_orchestrator()
    result = orch.execute_invoice(text, use_llm_agent=True, llm_call=fake_llm)
    assert result["decision"]["decision"] == "allow"
    assert result["execution"]["status"] == "paid"
    assert result["proposed_action"]["amount"]["value"] == "1240.00"
    assert dup.is_approved("INV-001")
