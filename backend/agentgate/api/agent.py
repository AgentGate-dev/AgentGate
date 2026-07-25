"""POST /agent/process — automated upstream + gate (no manual verify payload).

The browser demo and MCP upstream tools share the same parse → propose →
verify semantics: invoice text in, Decision out. This endpoint runs the gate
through the app's own injected dependencies (store, policy, system of record)
— never through the MCP module's singletons, and without importing the
optional ``mcp`` SDK at all.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agentgate.core.decision import fail_closed_decision
from agentgate.orchestrator.gate import run_gate_decision
from agentgate.upstream.invoice_text import parse_invoice_text, parsed_invoice_to_wire
from agentgate.upstream.pipeline import default_proposed_action

logger = logging.getLogger("agentgate.api.agent")

router = APIRouter(prefix="/agent", tags=["agent"])

MAX_RAW_TEXT = 500_000


class ProcessInvoiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_text: str = Field(min_length=1, max_length=MAX_RAW_TEXT)


@router.post("/process")
async def process_invoice_endpoint(
    body: ProcessInvoiceRequest, request: Request
) -> JSONResponse:
    """Parse invoice text, propose payment, and return the gate Decision."""
    try:
        parsed = parse_invoice_text(body.raw_text)
        proposed_action = default_proposed_action(parsed)
        source = {
            "invoice": parsed_invoice_to_wire(parsed),
            "raw_text": parsed.raw_text,
        }
        decision = run_gate_decision(
            proposed_action,
            source,
            store=request.app.state.store,
            policy=request.app.state.policy,
            source_of_record=request.app.state.source_of_record,
        )
        payload = {
            "parsed_invoice": parsed_invoice_to_wire(parsed),
            "proposed_action": proposed_action,
            "source_mode": "caller",
            "decision": decision,
        }
    except ValueError as exc:
        logger.info("upstream parse failed: %s", exc)
        decision = fail_closed_decision([str(exc)]).model_dump(mode="json")
        payload = {
            "parsed_invoice": None,
            "proposed_action": None,
            "source_mode": None,
            "decision": decision,
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("unexpected error in /agent/process")
        decision = fail_closed_decision([exc]).model_dump(mode="json")
        payload = {
            "parsed_invoice": None,
            "proposed_action": None,
            "source_mode": None,
            "decision": decision,
        }
    return JSONResponse(content=payload)
