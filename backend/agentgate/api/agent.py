"""POST /agent/process — automated upstream + gate (no manual verify payload).

The browser demo and MCP upstream tools share this path: invoice text in,
Decision out. The gate semantics are identical to ``verify_action`` / ``/verify``.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agentgate.core.decision import fail_closed_decision
from agentgate.core.schemas import Decision
from agentgate.upstream.pipeline import process_invoice

logger = logging.getLogger("agentgate.api.agent")

router = APIRouter(prefix="/agent", tags=["agent"])

MAX_RAW_TEXT = 500_000


class ProcessInvoiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_text: str = Field(min_length=1, max_length=MAX_RAW_TEXT)


@router.post("/process")
async def process_invoice_endpoint(body: ProcessInvoiceRequest) -> JSONResponse:
    """Parse invoice text, propose payment, and return the gate Decision."""
    try:
        payload = process_invoice(body.raw_text)
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
    else:
        payload["decision"] = Decision.model_validate(payload["decision"]).model_dump(mode="json")
    return JSONResponse(content=payload)
