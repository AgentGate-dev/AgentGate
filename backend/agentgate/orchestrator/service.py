"""Orchestrator — verify, then execute payment on ALLOW (enterprise execute layer).

Money-safety ordering (PRD §5b, D50 applied to the test rail): the duplicate
store is reserved BEFORE the payment rail runs, so a duplicate can never race
past the check; an execution failure after reserve keeps the invoice number
burned and surfaces a typed status for a human. Approval resolution is a
conditional first-resolution-wins update, and pending approvals expire.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from agentgate.core.duplicate_store import DuplicateStore, AlreadyApprovedError
from agentgate.core.policy import Policy
from agentgate.core.system_of_record import SourceOfRecord
from agentgate.orchestrator.gate import run_gate_decision
from agentgate.orchestrator.payment import PaymentProvider, build_payment_provider
from agentgate.orchestrator.store import OrchestratorStore
from agentgate.upstream.invoice_text import parse_invoice_text, parsed_invoice_to_wire
from agentgate.upstream.pipeline import default_proposed_action

DEFAULT_APPROVAL_TTL_HOURS = 72


def _approval_ttl_from_env() -> int:
    raw = os.environ.get("AGENTGATE_APPROVAL_TTL_HOURS", "").strip()
    if not raw:
        return DEFAULT_APPROVAL_TTL_HOURS
    value = int(raw)  # a malformed value should fail loudly at construction
    if value <= 0:
        raise ValueError("AGENTGATE_APPROVAL_TTL_HOURS must be a positive integer.")
    return value


class Orchestrator:
    """Chains upstream parse/propose, gate verify, payment execution, and audit."""

    def __init__(
        self,
        *,
        duplicate_store: DuplicateStore,
        orchestrator_store: OrchestratorStore,
        policy: Policy,
        source_of_record: Optional[SourceOfRecord],
        payment_provider: Optional[PaymentProvider] = None,
        clock: Optional[Callable[[], datetime]] = None,
        approval_ttl_hours: Optional[int] = None,
    ) -> None:
        self._duplicate_store = duplicate_store
        self._orchestrator_store = orchestrator_store
        self._policy = policy
        self._source_of_record = source_of_record
        self._payment = payment_provider or build_payment_provider()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._approval_ttl = timedelta(
            hours=approval_ttl_hours if approval_ttl_hours is not None else _approval_ttl_from_env()
        )

    def execute_invoice(self, raw_text: str) -> dict[str, Any]:
        """Parse invoice text, verify, and pay on ALLOW (test rail by default)."""
        parsed = parse_invoice_text(raw_text)
        proposed_action = default_proposed_action(parsed)
        source = {
            "invoice": parsed_invoice_to_wire(parsed),
            "raw_text": parsed.raw_text,
        }
        return self._execute_verified(proposed_action, source, parsed_invoice=parsed_invoice_to_wire(parsed))

    def approve_and_execute(self, approval_id: str, *, approved: bool) -> dict[str, Any]:
        """Human decision on a pending ESCALATE — pay only if approved, only
        once (first resolution wins), and only within the approval TTL."""
        pending = self._orchestrator_store.get_pending_approval(approval_id)
        if pending is None:
            raise ValueError(f"Unknown approval id {approval_id!r}.")
        if pending["status"] != "pending":
            raise ValueError(f"Approval {approval_id!r} is already {pending['status']!r}.")

        created_at = datetime.fromisoformat(pending["created_at"])
        if self._clock() - created_at > self._approval_ttl:
            self._orchestrator_store.expire_approval(approval_id)
            audit_id = self._orchestrator_store.record_audit(
                {
                    "gate_decision": "escalate",
                    "execution_status": "approval_expired",
                    "approval_id": approval_id,
                    "invoice_number": pending["invoice_number"],
                    "trace_id": pending.get("gate_trace_id"),
                }
            )
            return {
                "approval_id": approval_id,
                "execution": {"status": "approval_expired"},
                "audit_id": audit_id,
            }

        if not self._orchestrator_store.resolve_approval(approval_id, approved=approved):
            raise ValueError(f"Approval {approval_id!r} is already resolved.")
        if not approved:
            audit_id = self._orchestrator_store.record_audit(
                {
                    "gate_decision": "escalate",
                    "execution_status": "rejected_by_human",
                    "approval_id": approval_id,
                    "invoice_number": pending["invoice_number"],
                    "trace_id": pending.get("gate_trace_id"),
                }
            )
            return {
                "approval_id": approval_id,
                "execution": {"status": "rejected_by_human"},
                "audit_id": audit_id,
            }

        return self._execute_payment(
            pending["proposed_action"],
            pending["source"],
            gate_decision="escalate",
            gate_trace_id=pending.get("gate_trace_id"),
            approval_id=approval_id,
            human_override=True,
        )

    def _execute_verified(
        self,
        proposed_action: dict[str, Any],
        source: dict[str, Any],
        *,
        parsed_invoice: Optional[dict[str, Any]] = None,
        approval_id: Optional[str] = None,
    ) -> dict[str, Any]:
        decision = run_gate_decision(
            proposed_action,
            source,
            store=self._duplicate_store,
            policy=self._policy,
            source_of_record=self._source_of_record,
        )
        invoice_number = proposed_action.get("invoice_number") or (
            (parsed_invoice or {}).get("invoice_number")
        )
        trace_id = decision.get("trace_id")
        gate_outcome = decision["decision"]

        base: dict[str, Any] = {
            "parsed_invoice": parsed_invoice,
            "proposed_action": proposed_action,
            "source_mode": "caller" if "fetch" not in source else "system_of_record",
            "decision": decision,
        }

        if gate_outcome == "block":
            audit_id = self._orchestrator_store.record_audit(
                {
                    "trace_id": trace_id,
                    "invoice_number": invoice_number,
                    "gate_decision": gate_outcome,
                    "execution_status": "not_executed_blocked",
                    "approval_id": approval_id,
                    "decision": decision,
                }
            )
            base["execution"] = {"status": "not_executed_blocked"}
            base["audit_id"] = audit_id
            return base

        if gate_outcome == "escalate" and approval_id is None:
            pending_id = self._orchestrator_store.create_pending_approval(
                invoice_number=str(invoice_number or proposed_action.get("invoice_number", "")),
                proposed_action=proposed_action,
                source=source,
                gate_trace_id=trace_id,
            )
            audit_id = self._orchestrator_store.record_audit(
                {
                    "trace_id": trace_id,
                    "invoice_number": invoice_number,
                    "gate_decision": gate_outcome,
                    "execution_status": "pending_human_approval",
                    "approval_id": pending_id,
                    "decision": decision,
                }
            )
            base["execution"] = {
                "status": "pending_human_approval",
                "approval_id": pending_id,
            }
            base["audit_id"] = audit_id
            return base

        if gate_outcome != "allow":
            audit_id = self._orchestrator_store.record_audit(
                {
                    "trace_id": trace_id,
                    "invoice_number": invoice_number,
                    "gate_decision": gate_outcome,
                    "execution_status": "not_executed",
                    "approval_id": approval_id,
                    "decision": decision,
                }
            )
            base["execution"] = {"status": "not_executed"}
            base["audit_id"] = audit_id
            return base

        return self._execute_payment(
            proposed_action,
            source,
            gate_decision=gate_outcome,
            gate_trace_id=trace_id,
            approval_id=approval_id,
            decision=decision,
            parsed_invoice=parsed_invoice,
            human_override=False,
        )

    def _execute_payment(
        self,
        proposed_action: dict[str, Any],
        source: dict[str, Any],
        *,
        gate_decision: str,
        gate_trace_id: Optional[str],
        approval_id: Optional[str],
        decision: Optional[dict[str, Any]] = None,
        parsed_invoice: Optional[dict[str, Any]] = None,
        human_override: bool,
    ) -> dict[str, Any]:
        invoice_number = str(proposed_action.get("invoice_number") or "").strip()
        base: dict[str, Any] = {
            "parsed_invoice": parsed_invoice,
            "proposed_action": proposed_action,
            "decision": decision,
        }
        if not invoice_number:
            audit_id = self._orchestrator_store.record_audit(
                {
                    "trace_id": gate_trace_id,
                    "invoice_number": None,
                    "gate_decision": gate_decision,
                    "execution_status": "not_executed",
                    "approval_id": approval_id,
                    "error": "proposed action carries no invoice number",
                }
            )
            return {**base, "execution": {"status": "not_executed"}, "audit_id": audit_id}

        # Reserve FIRST (D50): mark_approved raising AlreadyApprovedError is the
        # single duplicate authority — checked-then-paid would leave a window in
        # which two concurrent executions both pass the check and both pay.
        try:
            self._duplicate_store.mark_approved(
                invoice_number,
                approved_at=self._clock().isoformat(),
            )
        except AlreadyApprovedError:
            audit_id = self._orchestrator_store.record_audit(
                {
                    "trace_id": gate_trace_id,
                    "invoice_number": invoice_number,
                    "gate_decision": gate_decision,
                    "execution_status": "payment_aborted_duplicate",
                    "approval_id": approval_id,
                    "decision": decision,
                }
            )
            return {
                **base,
                "execution": {"status": "payment_aborted_duplicate"},
                "audit_id": audit_id,
            }

        try:
            payment = self._payment.execute(proposed_action)
        except Exception as exc:  # noqa: BLE001 — rail failure must not crash the caller
            # Unknown outcome after reserve: the number STAYS burned and a human
            # investigates (unmarking would reopen the double-pay window).
            audit_id = self._orchestrator_store.record_audit(
                {
                    "trace_id": gate_trace_id,
                    "invoice_number": invoice_number,
                    "gate_decision": gate_decision,
                    "execution_status": "allowed_execution_failed",
                    "approval_id": approval_id,
                    "error": str(exc)[:300],
                }
            )
            return {
                **base,
                "execution": {"status": "allowed_execution_failed", "error": str(exc)[:300]},
                "audit_id": audit_id,
            }

        status = "paid_after_human_approval" if human_override else "paid"
        audit_id = self._orchestrator_store.record_audit(
            {
                "trace_id": gate_trace_id,
                "invoice_number": invoice_number,
                "gate_decision": gate_decision,
                "execution_status": status,
                "payment_id": payment.payment_id,
                "approval_id": approval_id,
                "decision": decision,
                "payment": asdict(payment),
                "human_override": human_override,
            }
        )
        return {
            **base,
            "execution": {
                "status": status,
                "payment": asdict(payment),
            },
            "audit_id": audit_id,
        }
