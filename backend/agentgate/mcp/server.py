"""AgentGate as an MCP server (PRD SS10 Slices 8 + 10, D44/D49–D53).

Two tools under the SAME envelope and fail-closed contract as the HTTP
boundary (D35): requests validate through ``VerifyRequest`` (bounds,
``extra="forbid"``, po rejected), the tools ALWAYS return a dict and never
raise to the MCP client (an MCP tool *error* would sit outside the Decision
vocabulary and invite the calling agent to retry or route around the gate),
and the boundary fields are stamped identically.

- ``verify_action`` — the ADVISORY tier: Decision only, read-only.
- ``pay_invoice`` — the ENFORCED tier (D49): the same verification, and on
  ALLOW a signed single-use decision token is minted, consumed, the invoice
  number reserved, and only then the registered executor runs. Execution is
  structurally unreachable except through a valid token the gate minted.

Run it over stdio: ``agentgate-mcp`` (console script) or
``python -m agentgate.mcp.server``.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from agentgate.core.decision import decide, fail_closed_decision
from agentgate.core.duplicate_store import (
    AlreadyApprovedError,
    DuplicateStore,
    TokenAlreadyConsumedError,
)
from agentgate.core.execution import (
    ExecutionRequest,
    SigningKeyError,
    TokenError,
    canonical_action_dump,
    default_executors,
    mint_token,
    signing_key_from_env,
    utcnow,
    verify_token,
)
from agentgate.core.policy import DEFAULT_POLICY
from agentgate.core.schemas import ProposedAction, VerifyRequest
from agentgate.core.system_of_record import (
    SourceOfRecordError,
    build_source_of_record,
    resolve_source,
    system_of_record_evidence,
)
from agentgate.core.tracing import build_tracer, record_safely

logger = logging.getLogger("agentgate.mcp")

mcp = FastMCP("agentgate")


class ExecutionUnavailableError(ValueError):
    """An execution precondition is not met (durable store). pay_invoice
    fail-closes instructively; the advisory tier is unaffected (D52)."""


# Server-process singletons, wired exactly like the HTTP app (D38/D37): the
# store default is in-memory (AGENTGATE_DB_PATH opts into a file), tracing is
# a no-op without LANGFUSE keys. The executor registry and clock are module
# seams (D53; clock injected as a callable — no clock-mocking dependency).
_store = DuplicateStore(os.environ.get("AGENTGATE_DB_PATH", ":memory:"))
_tracer = build_tracer()
_policy = DEFAULT_POLICY
_source_of_record = build_source_of_record()
_executors = default_executors()
_clock = utcnow


@mcp.tool()
def verify_action(proposed_action: dict, source: dict) -> dict:
    """Verify a proposed action against caller-supplied evidence.

    Returns an AgentGate Decision: ``decision`` is allow | block | escalate,
    with machine-readable ``reasons`` (on a block, ``field_to_change`` and
    ``expected`` say exactly what to fix), a checks table, and a grounding
    score. ``source`` either contains a structured ``invoice`` and optional
    ``raw_text`` (the original invoice text) for grounding — **required** in caller
    mode; structured JSON alone is rejected. Or — when the server is configured
    with a system of record — ``{"fetch": "INV-001"}`` to have AgentGate resolve the invoice itself; fetched decisions mark every
    ``evidence_used`` entry with a ``system_of_record:`` prefix. All money
    values MUST be JSON strings (``"1240.00"``), never numbers — a JSON number
    has already been parsed into a lossy float by the transport and will be
    rejected into a fail-closed escalate (AgentGate keeps money exact). A
    passing decision means "consistent with the evidence provided," never
    "the payment is correct or authorized."
    """
    started = time.perf_counter()
    trace_input: dict
    try:
        req = VerifyRequest.model_validate(
            {"proposed_action": proposed_action, "source": source}
        )
        resolved = resolve_source(req.source, _source_of_record)
        raw_text = resolved.raw_text
        is_duplicate = _store.is_approved(resolved.invoice.invoice_number)
        decision = decide(
            resolved.invoice,
            req.proposed_action,
            policy=_policy,
            raw_text=raw_text,
            is_duplicate=is_duplicate,
        )
        if resolved.fetched:
            # Provenance is a boundary fact (D45), stamped here like trace_id.
            decision = decision.model_copy(
                update={"evidence_used": system_of_record_evidence(decision.evidence_used)}
            )
        trace_input = {
            "invoice": resolved.invoice.model_dump(mode="json"),
            "proposed_action": req.proposed_action.model_dump(mode="json"),
            "raw_text_length": None if raw_text is None else len(raw_text),
            "source_mode": "system_of_record" if resolved.fetched else "caller",
        }
    except SourceOfRecordError as exc:
        decision = fail_closed_decision([str(exc)])
        trace_input = {"validated": False}
    except ValidationError as exc:
        decision = fail_closed_decision([exc])
        trace_input = {"validated": False}
    except Exception as exc:  # noqa: BLE001 — never raise to the MCP client (D44)
        logger.exception("unexpected error in verify_action; failing closed")
        decision = fail_closed_decision([exc])
        trace_input = {"validated": False}

    decision = decision.model_copy(
        update={
            "trace_id": str(uuid.uuid4()),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    payload = decision.model_dump(mode="json")
    record_safely(
        _tracer,
        trace_id=decision.trace_id,
        name="verify_action",
        input=trace_input,
        output=payload,
    )
    return payload


def _execute_allowed(action: ProposedAction, trace_id: str, key: str) -> dict:
    """The enforced execution path, in the locked D50 order:
    **consume token → mark_approved → execute.** Reservation before execution
    closes the TOCTOU single-use tokens cannot: two concurrent verified
    decisions hold two *different* valid tokens, so only the second
    ``mark_approved`` raising can stop the second payment — and that refusal
    happens BEFORE money moves."""
    try:
        token = mint_token(
            action,
            decision="allow",
            trace_id=trace_id,
            key=key,
            ttl_seconds=_policy.execution.token_ttl_seconds,
            now=_clock,
        )
        payload = verify_token(token, key=key, now=_clock)
        # D51: the executor re-checks that what it is about to execute is
        # exactly the canonical action the token was minted for.
        if payload.get("action") != json.loads(canonical_action_dump(action)):
            raise TokenError("token action does not match the action about to execute.")
        _store.consume_token(str(payload["trace_id"]), consumed_at=_clock().isoformat())
    except TokenAlreadyConsumedError as exc:
        return {"status": "refused_replay", "executed": False, "error": str(exc)[:300]}
    except TokenError as exc:
        return {"status": "refused_token", "executed": False, "error": str(exc)[:300]}

    try:
        _store.mark_approved(action.invoice_number, approved_at=_clock().isoformat())
    except AlreadyApprovedError:
        # The concurrent-double-pay refusal: before any execution (D50).
        return {"status": "payment_aborted_duplicate", "executed": False}

    executor = _executors.get(getattr(action.action_type, "value", str(action.action_type)))
    if executor is None:
        # Defensive dead-man code (D53): the frame stage escalates every other
        # action_type before an ALLOW can exist. Refuse, never crash.
        return {"status": "refused_no_executor", "executed": False}
    try:
        result = executor.execute(ExecutionRequest(action=action, trace_id=trace_id))
    except Exception as exc:  # noqa: BLE001 — an executor failure must not crash the tool
        logger.exception("executor failed after reservation; invoice stays reserved")
        # Unknown-outcome money movement: the invoice number STAYS reserved —
        # a human investigates before any retry (no auto-retry, no unmark API).
        return {
            "status": "allowed_execution_failed",
            "executed": False,
            "error": str(exc)[:300],
        }
    return {
        "status": "executed",
        "executed": result.executed,
        "executor": result.executor,
        "reference": result.reference,
    }


@mcp.tool()
def pay_invoice(proposed_action: dict, source: dict) -> dict:
    """Verify a proposed payment and, ONLY on allow, execute it.

    The enforced tier (``verify_action`` is the advisory one): the same
    envelope, evidence rules, and Decision vocabulary, but an ``allow`` mints a
    signed single-use decision token, reserves the invoice number in the
    duplicate store, and runs the registered executor — in that order, so a
    duplicate or a replay is refused BEFORE any money moves. Returns
    ``{"decision": Decision, "execution": {...} | null}``; the Decision is
    never mutated by execution outcomes. Requires ``AGENTGATE_SIGNING_KEY``
    (>= 32 chars) and a file-backed store (``AGENTGATE_DB_PATH``) — without
    them the tool fails closed with an instructive escalate and executes
    nothing. Money values MUST be JSON strings, never numbers (see
    ``verify_action``).
    """
    started = time.perf_counter()
    req: Optional[VerifyRequest] = None
    key = ""
    trace_input: dict = {"validated": False}
    try:
        key = signing_key_from_env()
        if not _store.is_file_backed:
            raise ExecutionUnavailableError(
                "Execution requires a durable, file-backed store — set "
                "AGENTGATE_DB_PATH. Consumed tokens and reservations are the "
                "double-pay defense and must survive a restart; the advisory "
                "verify_action tool keeps working in-memory."
            )
        req = VerifyRequest.model_validate(
            {"proposed_action": proposed_action, "source": source}
        )
        resolved = resolve_source(req.source, _source_of_record)
        raw_text = resolved.raw_text
        is_duplicate = _store.is_approved(resolved.invoice.invoice_number)
        decision = decide(
            resolved.invoice,
            req.proposed_action,
            policy=_policy,
            raw_text=raw_text,
            is_duplicate=is_duplicate,
        )
        if resolved.fetched:
            decision = decision.model_copy(
                update={"evidence_used": system_of_record_evidence(decision.evidence_used)}
            )
        trace_input = {
            "invoice": resolved.invoice.model_dump(mode="json"),
            "proposed_action": req.proposed_action.model_dump(mode="json"),
            "raw_text_length": None if raw_text is None else len(raw_text),
            "source_mode": "system_of_record" if resolved.fetched else "caller",
        }
    except (SigningKeyError, ExecutionUnavailableError) as exc:
        decision = fail_closed_decision([str(exc)])
        req = None
    except SourceOfRecordError as exc:
        decision = fail_closed_decision([str(exc)])
        req = None
    except ValidationError as exc:
        decision = fail_closed_decision([exc])
        req = None
    except Exception as exc:  # noqa: BLE001 — never raise to the MCP client (D44/D49)
        logger.exception("unexpected error in pay_invoice; failing closed")
        decision = fail_closed_decision([exc])
        req = None

    # Boundary fields are stamped BEFORE minting so the token binds the real
    # trace_id of this decision.
    decision = decision.model_copy(
        update={
            "trace_id": str(uuid.uuid4()),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    decision_payload = decision.model_dump(mode="json")

    execution: Optional[dict] = None
    if req is not None and decision_payload["decision"] == "allow":
        execution = _execute_allowed(req.proposed_action, decision.trace_id, key)

    outcome = {"decision": decision_payload, "execution": execution}
    record_safely(
        _tracer,
        trace_id=decision.trace_id,
        name="pay_invoice",
        input=trace_input,
        output=outcome,
    )
    return outcome


def main() -> None:
    """Entry point for the ``agentgate-mcp`` console script: serve over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
