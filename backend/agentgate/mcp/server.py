"""AgentGate as an MCP server (PRD SS10 Slice 8, D44).

One tool — ``verify_action`` — wrapping the pure ``decide()`` in-process under
the SAME envelope and fail-closed contract as the HTTP boundary (D35): the
request validates through ``VerifyRequest`` (bounds, ``extra="forbid"``, po
rejected), the tool ALWAYS returns a Decision dict and never raises to the MCP
client (an MCP tool *error* would sit outside the Decision vocabulary and
invite the calling agent to retry or route around the gate), and the boundary
fields are stamped identically. The duplicate store and tracing wire from the
same environment variables as the HTTP app; ``/verify`` semantics apply — the
tool is read-only.

Run it over stdio: ``agentgate-mcp`` (console script) or
``python -m agentgate.mcp.server``.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP
from pydantic import ValidationError

from agentgate.core.decision import decide, fail_closed_decision
from agentgate.core.duplicate_store import DuplicateStore
from agentgate.core.policy import DEFAULT_POLICY
from agentgate.core.schemas import VerifyRequest
from agentgate.core.system_of_record import (
    SourceOfRecordError,
    build_source_of_record,
    resolve_source,
    system_of_record_evidence,
)
from agentgate.core.tracing import build_tracer, record_safely

logger = logging.getLogger("agentgate.mcp")

mcp = FastMCP("agentgate")

# Server-process singletons, wired exactly like the HTTP app (D38/D37): the
# store default is in-memory (AGENTGATE_DB_PATH opts into a file), tracing is
# a no-op without LANGFUSE keys.
_store = DuplicateStore(os.environ.get("AGENTGATE_DB_PATH", ":memory:"))
_tracer = build_tracer()
_policy = DEFAULT_POLICY
_source_of_record = build_source_of_record()


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


def main() -> None:
    """Entry point for the ``agentgate-mcp`` console script: serve over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
