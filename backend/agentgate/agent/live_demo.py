"""End-to-end live demo — real LLM agent proposes, gate verifies, test rail pays.

    python -m agentgate.agent.live_demo [invoice.txt]

Requires ``pip install -e ".[agent,llm,server]"`` and a provider key (``GEMINI_API_KEY``
for the default model). Without a file argument, runs the Acme sample invoice.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from agentgate.core.duplicate_store import DuplicateStore
from agentgate.core.policy import DEFAULT_POLICY
from agentgate.orchestrator.payment import TestPaymentProvider
from agentgate.orchestrator.service import Orchestrator
from agentgate.orchestrator.store import OrchestratorStore


def _default_invoice_path() -> Path:
    repo = Path(__file__).resolve().parents[3]
    return repo / "frontend" / "public" / "invoices" / "acme-inv-001.txt"


def run_live_demo(raw_text: str) -> dict:
    """Run parse → LLM propose → gate → test payment."""
    orch = Orchestrator(
        duplicate_store=DuplicateStore(),
        orchestrator_store=OrchestratorStore(),
        policy=DEFAULT_POLICY,
        source_of_record=None,
        payment_provider=TestPaymentProvider(),
    )
    return orch.execute_invoice(raw_text, use_llm_agent=True)


def main() -> None:
    if importlib.util.find_spec("litellm") is None:
        raise SystemExit(
            "Live demo needs real model calls: pip install -e '.[agent,llm,server]' "
            "and set GEMINI_API_KEY (or AGENTGATE_LLM_MODEL plus that provider's key)."
        )

    path = Path(sys.argv[1]) if len(sys.argv) > 1 else _default_invoice_path()
    if not path.is_file():
        raise SystemExit(f"Invoice file not found: {path}")

    raw_text = path.read_text(encoding="utf-8")
    print(f"=== AgentGate live demo ===")
    print(f"Invoice file: {path.name}\n")

    result = run_live_demo(raw_text)
    proposal = result.get("proposed_action") or {}
    decision = result.get("decision") or {}
    execution = result.get("execution") or {}

    if result.get("agent_error"):
        print(f"Agent error: {result['agent_error']}\n")

    if proposal:
        amount = proposal.get("amount") or {}
        print(
            f"Agent proposes: {proposal.get('action_type')} "
            f"{proposal.get('invoice_number')} "
            f"for {amount.get('value')} {amount.get('currency')} "
            f"to {proposal.get('vendor')}"
        )
    print()

    print(f"Gate decision: {decision.get('decision', 'unknown').upper()}")
    score = decision.get("score")
    print(f"Score: {score if score is not None else 'not computed'}")
    for reason in decision.get("reasons") or []:
        print(f"  reason: {reason.get('message')}")
    print()

    status = execution.get("status", "unknown")
    print(f"Execution: {status}")
    payment = execution.get("payment")
    if isinstance(payment, dict) and payment.get("payment_id"):
        print(f"  payment_id: {payment['payment_id']}")
    approval_id = execution.get("approval_id")
    if approval_id:
        print(f"  approval_id: {approval_id} (human review required)")
    print()

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
