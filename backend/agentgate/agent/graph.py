"""LangGraph demo agent + human-in-the-loop (DECISIONS D30).

A thin graph over the real gate:

    propose --(valid)--> gate --(allowed)--> END
        |                  |
     (failed)          (escalate / cap)
        \\----------------> human (interrupt: approve | reject) --> END

The agent is given a structured Invoice and proposes a payment action via the LLM
(the router is the only mock seam). The retry loop is the generalized
``run_with_retry`` with a VALUE-ONLY LLM re-proposer (D30): the loop owns which
field changes; the re-proposer returns only the new value, so the LLM can never
declare an adjustment (BLOCK->ESCALATE) or drift action_type/invoice_number.

Fail-closed (D11): a malformed LLM proposal or re-proposal routes to a human,
never a crash or an infinite loop. The gate's Decision is never rewritten by a
human approval (D25) — approval is the graph's resolution, recorded alongside the
untouched (ESCALATE) verdict.

Graph state is JSON-safe (plain dicts, not pydantic models): it survives any
checkpointer without relying on serde type-reconstruction, and it is exactly what
the Slice 7 API/dashboard will consume. Pydantic lives in the core and is
reconstructed inside nodes.
"""

from __future__ import annotations

from typing import Callable, Optional, TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import ValidationError

from ..core.llm_json import ExtractionError, parse_llm_json
from ..core.llm_router import LLMRouterError, call_llm
from ..core.policy import DEFAULT_POLICY, Policy
from ..core.retry import Resolution, UnfixableBlockError, run_with_retry
from ..core.schemas import Invoice, Money, ProposedAction

_PROPOSE_PROMPT = (
    "You are an accounts-payable agent. Given this invoice, propose a payment "
    "action. Respond with ONLY a JSON object of the form "
    '{{"action_type": "approve_payment", "invoice_number": "<number>", '
    '"amount": {{"value": "<amount as string>", "currency": "<ISO code>"}}, '
    '"vendor": "<vendor>"}}. Do not include any other text.\n\nInvoice:\n{invoice_json}'
)

_REPROPOSE_PROMPT = (
    "A verification gate blocked your proposed payment and told you exactly what "
    "to change. Reason: {message}\nField to correct: {field}\nReply with ONLY a "
    'JSON object {{"value": "<amount as string>", "currency": "<ISO code>"}} '
    "giving the corrected value for that field. No other text.\n\nInvoice:\n{invoice_json}"
)


class AgentState(TypedDict, total=False):
    """JSON-safe graph state (see module docstring)."""

    invoice: dict
    action: Optional[dict]
    decision: Optional[dict]  # the gate verdict as JSON, verbatim (never rewritten, D25)
    attempts: int
    resolution: Optional[str]
    human_decision: Optional[str]
    outcome: Optional[str]  # allowed | approved_by_human | rejected_by_human
    error: Optional[str]


def initial_state(invoice: Invoice) -> AgentState:
    """Build the graph's initial state from a pydantic Invoice (JSON-safe)."""
    return {"invoice": invoice.model_dump(mode="json")}


def propose_payment_action(
    invoice: Invoice, *, llm_call: Callable[[str], str] = call_llm
) -> ProposedAction:
    """Ask the LLM to propose a payment action for ``invoice``. Raises
    ``LLMRouterError``/``ExtractionError``/``ValidationError`` on failure — the
    caller fails closed (D11)."""
    raw = llm_call(_PROPOSE_PROMPT.format(invoice_json=invoice.model_dump_json()))
    return ProposedAction.model_validate(parse_llm_json(raw))


def _propose_action(invoice: Invoice, *, llm_call: Callable[[str], str]) -> ProposedAction:
    """Internal alias kept for the graph node."""
    return propose_payment_action(invoice, llm_call=llm_call)


def _make_reproposer(llm_call: Callable[[str], str]):
    """Build a VALUE-ONLY re-proposer for ``run_with_retry`` (D30). It returns
    only the corrected value for the field the block reason names — a ``Money``
    for the amount misread, the sole agent-fixable block in v1. Any failure
    (router, parse, invalid value) becomes ``UnfixableBlockError`` so the loop
    routes to a human instead of crashing or looping (D11)."""

    def _repropose(invoice: Invoice, action: ProposedAction, reason) -> object:
        prompt = _REPROPOSE_PROMPT.format(
            message=reason.message,
            field=reason.field_to_change,
            invoice_json=invoice.model_dump_json(),
        )
        try:
            payload = parse_llm_json(llm_call(prompt))
            return Money(value=payload["value"], currency=payload["currency"])
        except (LLMRouterError, ExtractionError, KeyError, TypeError, ValueError) as exc:
            raise UnfixableBlockError(f"LLM re-proposal failed: {exc}") from exc

    return _repropose


def build_agent(*, llm_call: Callable[[str], str] = call_llm, policy: Policy = DEFAULT_POLICY):
    """Compile the demo agent graph. ``llm_call`` is the injected router seam
    (mocked in tests); ``policy`` supplies the retry cap."""

    def propose(state: AgentState) -> dict:
        invoice = Invoice.model_validate(state["invoice"])
        try:
            action = _propose_action(invoice, llm_call=llm_call)
        except (LLMRouterError, ExtractionError, ValidationError) as exc:
            return {"action": None, "error": f"agent could not form a valid proposal: {exc}"}
        return {"action": action.model_dump(mode="json")}

    def gate(state: AgentState) -> dict:
        invoice = Invoice.model_validate(state["invoice"])
        action = ProposedAction.model_validate(state["action"])
        outcome = run_with_retry(
            invoice,
            action,
            max_attempts=policy.retry.max_attempts,
            policy=policy,  # thread the full policy so its escalation thresholds govern the loop
            propose_value=_make_reproposer(llm_call),
        )
        update: dict = {
            "decision": outcome.final_decision.model_dump(mode="json"),
            "attempts": outcome.attempts,
            "resolution": outcome.resolution.value,
        }
        if outcome.resolution is Resolution.allowed:
            update["outcome"] = "allowed"
        return update

    def human(state: AgentState) -> dict:
        reply = interrupt(
            {
                "reason": "human review required",
                "resolution": state.get("resolution"),
                "decision": state.get("decision"),
                "error": state.get("error"),
            }
        )
        approved = str(reply).strip().lower() == "approve"
        return {
            "human_decision": "approve" if approved else "reject",
            "outcome": "approved_by_human" if approved else "rejected_by_human",
        }

    def after_propose(state: AgentState) -> str:
        return "gate" if state.get("action") is not None else "human"

    def after_gate(state: AgentState) -> str:
        return END if state.get("resolution") == "allowed" else "human"

    graph = StateGraph(AgentState)
    graph.add_node("propose", propose)
    graph.add_node("gate", gate)
    graph.add_node("human", human)
    graph.add_edge(START, "propose")
    graph.add_conditional_edges("propose", after_propose, {"gate": "gate", "human": "human"})
    graph.add_conditional_edges("gate", after_gate, {END: END, "human": "human"})
    graph.add_edge("human", END)
    return graph.compile(checkpointer=MemorySaver())
