"""Standalone invoice payment agent — a REAL agent that uses AgentGate as an external gate.

This process is **separate from AgentGate**. It owns the LLM, reads invoices, and
proposes payments. AgentGate is only contacted over HTTP:

  1. ``POST /verify`` — gate checks the proposal against evidence
  2. ``POST /orchestrator/pay`` — payment executes ONLY if the gate allows
  3. ``POST /orchestrator/approvals/{id}/decide`` — human override on ESCALATE

The agent never holds payment credentials; the orchestrator does.

**Start AgentGate first** (in another terminal):

    cd backend && source .venv/bin/activate
    set -a && source .env && set +a
    uvicorn agentgate.main:app --host 127.0.0.1 --port 8000

**Then run this agent**:

    AGENTGATE_URL=http://127.0.0.1:8000 python -m agentgate.agent.invoice_payment_agent

    # or with a specific invoice:
    AGENTGATE_URL=http://127.0.0.1:8000 python -m agentgate.agent.invoice_payment_agent path/to/invoice.txt
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from agentgate.agent.graph import propose_payment_action
from agentgate.core.llm_json import ExtractionError, parse_llm_json
from agentgate.core.llm_router import LLMRouterError, call_llm
from agentgate.core.policy import DEFAULT_POLICY
from agentgate.core.schemas import BlockReason, BlockType, Invoice, Money, ProposedAction
from agentgate.upstream.invoice_text import parse_invoice_text, parsed_invoice_to_wire

_REPROPOSE_PROMPT = (
    "A verification gate blocked your proposed payment and told you exactly what "
    "to change. Reason: {message}\nField to correct: {field}\nReply with ONLY a "
    'JSON object {{"value": "<amount as string>", "currency": "<ISO code>"}} '
    "giving the corrected value for that field. No other text.\n\nInvoice:\n{invoice_json}"
)


def _default_invoice_path() -> Path:
    repo = Path(__file__).resolve().parents[3]
    return repo / "frontend" / "public" / "invoices" / "acme-inv-001.txt"


def _gate_url(base: str, path: str) -> str:
    return f"{base.rstrip('/')}{path}"


def _post_json(url: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req = Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=120) as resp:  # noqa: S310 — demo client to configured gate URL
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except URLError as exc:
        raise RuntimeError(
            f"Could not reach AgentGate at {url}. Is the server running? ({exc.reason})"
        ) from exc


def _first_block_reason(decision: dict[str, Any]) -> Optional[BlockReason]:
    for raw in decision.get("reasons") or []:
        try:
            return BlockReason.model_validate(raw)
        except Exception:
            continue
    return None


def _apply_value_fix(action: ProposedAction, field: str, value: object) -> ProposedAction:
    if field == "amount":
        return action.model_copy(update={"amount": Money.model_validate(value)})
    if field == "vendor":
        return action.model_copy(update={"vendor": str(value)})
    raise ValueError(f"cannot fix field {field!r}")


def _repropose_value(
    invoice: Invoice,
    action: ProposedAction,
    reason: BlockReason,
    *,
    llm_call: Callable[[str], str],
) -> Money:
    prompt = _REPROPOSE_PROMPT.format(
        message=reason.message,
        field=reason.field_to_change,
        invoice_json=invoice.model_dump_json(),
    )
    payload = parse_llm_json(llm_call(prompt))
    return Money(value=payload["value"], currency=payload["currency"])


class InvoicePaymentAgent:
    """An accounts-payable agent that must pass AgentGate before paying."""

    def __init__(
        self,
        *,
        gate_base_url: str,
        llm_call: Callable[[str], str] = call_llm,
        policy=DEFAULT_POLICY,
        ask: Callable[[str], str] = input,
        out: Callable[[str], None] = print,
    ) -> None:
        self._gate = gate_base_url.rstrip("/")
        self._llm = llm_call
        self._policy = policy
        self._ask = ask
        self._out = out

    def run(self, raw_text: str) -> dict[str, Any]:
        """Read invoice text, propose via LLM, verify through AgentGate, pay on ALLOW."""
        parsed = parse_invoice_text(raw_text)
        invoice_wire = parsed_invoice_to_wire(parsed)
        invoice = Invoice.model_validate(invoice_wire)
        source = {"invoice": invoice_wire, "raw_text": parsed.raw_text}

        self._out(f"Agent read invoice {invoice.invoice_number} — total {invoice.total.value} {invoice.total.currency}")

        try:
            action = propose_payment_action(invoice, llm_call=self._llm)
        except (LLMRouterError, ExtractionError, ValueError) as exc:
            self._out(f"Agent could not form a proposal: {exc}")
            return {"outcome": "proposal_failed", "error": str(exc)}

        action = action.model_copy(
            update={
                "adjustments": [],
                "agent_rationale": "Agent proposes payment after reading the invoice.",
            }
        )
        self._out(
            f"Agent proposes: {action.action_type} {action.invoice_number} "
            f"for {action.amount.value} {action.amount.currency} to {action.vendor}"
        )

        decision, action = self._verify_with_retries(invoice, action, source)
        outcome = decision.get("decision")

        if outcome == "block":
            self._out("Gate blocked the proposal and the agent could not self-correct.")
            return {"outcome": "blocked", "decision": decision, "proposed_action": action.model_dump(mode="json")}

        if outcome == "escalate":
            self._out(f"Gate ESCALATE — score {decision.get('score', 'not computed')}")
            for reason in decision.get("reasons") or []:
                self._out(f"  reason: {reason.get('message')}")
            return self._pay_or_queue_human(action, source, decision)

        if outcome == "allow":
            self._out(f"Gate ALLOW — score {decision.get('score')}")
            return self._pay_or_queue_human(action, source, decision)

        self._out(f"Unexpected gate outcome: {outcome}")
        return {"outcome": "unknown", "decision": decision}

    def _verify_with_retries(
        self,
        invoice: Invoice,
        action: ProposedAction,
        source: dict[str, Any],
    ) -> tuple[dict[str, Any], ProposedAction]:
        max_attempts = self._policy.retry.max_attempts
        decision: dict[str, Any] = {}

        for attempt in range(1, max_attempts + 1):
            self._out(f"Agent calling AgentGate /verify (attempt {attempt}/{max_attempts})…")
            decision = _post_json(
                _gate_url(self._gate, "/verify"),
                {
                    "proposed_action": action.model_dump(mode="json"),
                    "source": source,
                },
            )
            outcome = decision.get("decision")
            self._out(f"  gate says: {outcome.upper() if outcome else 'unknown'}")

            if outcome != "block":
                return decision, action

            reason = _first_block_reason(decision)
            if (
                reason is None
                or reason.block_type != BlockType.agent_fixable
                or not reason.field_to_change
            ):
                return decision, action

            if attempt >= max_attempts:
                return decision, action

            self._out(f"  fixable block: {reason.message}")
            try:
                if reason.field_to_change.endswith(".amount") or reason.field_to_change == "amount":
                    fixed = _repropose_value(invoice, action, reason, llm_call=self._llm)
                    action = _apply_value_fix(action, "amount", fixed.model_dump(mode="json"))
                else:
                    return decision, action
            except (LLMRouterError, ExtractionError, KeyError, TypeError, ValueError) as exc:
                self._out(f"  agent could not fix the block: {exc}")
                return decision, action

            self._out(
                f"  agent corrected proposal → {action.amount.value} {action.amount.currency}"
            )

        return decision, action

    def _pay_or_queue_human(
        self,
        action: ProposedAction,
        source: dict[str, Any],
        prior_decision: dict[str, Any],
    ) -> dict[str, Any]:
        self._out("Agent calling AgentGate /orchestrator/pay (gate re-verifies, then pays)…")
        pay_result = _post_json(
            _gate_url(self._gate, "/orchestrator/pay"),
            {
                "proposed_action": action.model_dump(mode="json"),
                "source": source,
            },
        )
        execution = pay_result.get("execution") or {}
        status = execution.get("status")
        self._out(f"  execution: {status}")

        if status == "paid":
            payment = execution.get("payment") or {}
            self._out(f"  payment_id: {payment.get('payment_id')}")
            return {
                "outcome": "paid",
                "decision": pay_result.get("decision", prior_decision),
                "execution": execution,
            }

        if status == "pending_human_approval":
            approval_id = execution.get("approval_id")
            self._out("  human review required — the gate escalated this payment")
            reply = self._ask("  approve or reject? ").strip().lower()
            approved = reply in {"approve", "yes", "y"}
            self._out(f"  submitting human decision: {'approve' if approved else 'reject'}")
            resolved = _post_json(
                _gate_url(self._gate, f"/orchestrator/approvals/{approval_id}/decide"),
                {"approve": approved},
            )
            final_status = (resolved.get("execution") or {}).get("status")
            self._out(f"  final execution: {final_status}")
            if final_status == "paid_after_human_approval":
                payment = (resolved.get("execution") or {}).get("payment") or {}
                self._out(f"  payment_id: {payment.get('payment_id')}")
            return {
                "outcome": "approved_by_human" if approved else "rejected_by_human",
                "decision": prior_decision,
                "execution": resolved.get("execution"),
            }

        if status == "not_executed_blocked":
            self._out("  payment refused — gate blocked at execution boundary")
            return {"outcome": "blocked_at_pay", "decision": pay_result.get("decision"), "execution": execution}

        return {"outcome": status or "unknown", "decision": pay_result.get("decision"), "execution": execution}


def main() -> None:
    if importlib.util.find_spec("litellm") is None:
        raise SystemExit(
            "This agent makes real LLM calls: pip install -e '.[agent,llm,server]' "
            "and set GEMINI_API_KEY."
        )

    gate_url = os.environ.get("AGENTGATE_URL", "http://127.0.0.1:8000")
    auto_approve = os.environ.get("AGENT_AUTO_APPROVE", "").lower() in {"1", "true", "yes"}
    if "--auto-approve" in sys.argv:
        auto_approve = True
        sys.argv = [a for a in sys.argv if a != "--auto-approve"]
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else _default_invoice_path()
    if not path.is_file():
        raise SystemExit(f"Invoice file not found: {path}")

    raw_text = path.read_text(encoding="utf-8")
    print("=== Invoice payment agent (external to AgentGate) ===")
    print(f"AgentGate URL: {gate_url}")
    print(f"Invoice file:  {path.name}\n")

    agent = InvoicePaymentAgent(
        gate_base_url=gate_url,
        ask=(lambda _prompt: "approve") if auto_approve else input,
    )
    result = agent.run(raw_text)
    print(f"\n=== Done: {result.get('outcome')} ===")


if __name__ == "__main__":
    main()
