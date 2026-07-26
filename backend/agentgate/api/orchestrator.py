"""Orchestrator HTTP API — verify then execute (enterprise execute layer)."""

from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from agentgate.core.decision import fail_closed_decision
from agentgate.orchestrator.payment import TestPaymentProvider
from agentgate.orchestrator.service import Orchestrator
from agentgate.orchestrator.store import OrchestratorStore

logger = logging.getLogger("agentgate.api.orchestrator")

router = APIRouter(prefix="/orchestrator", tags=["orchestrator"])

MAX_RAW_TEXT = 500_000


class ExecuteInvoiceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    raw_text: str = Field(min_length=1, max_length=MAX_RAW_TEXT)
    use_llm_agent: bool = False


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool


class PayProposalRequest(BaseModel):
    """Agent-submitted proposal — the gate re-verifies before any payment."""

    model_config = ConfigDict(extra="forbid")

    proposed_action: dict
    source: dict


def _orchestrator_store(request: Request) -> OrchestratorStore:
    store = getattr(request.app.state, "orchestrator_store", None)
    if store is None:
        raise RuntimeError("orchestrator_store is not configured on the app.")
    return store


def _orchestrator(request: Request) -> Orchestrator:
    return Orchestrator(
        duplicate_store=request.app.state.store,
        orchestrator_store=_orchestrator_store(request),
        policy=request.app.state.policy,
        source_of_record=request.app.state.source_of_record,
        payment_provider=TestPaymentProvider(),
    )


@router.post("/execute")
async def execute_invoice(body: ExecuteInvoiceRequest, request: Request) -> JSONResponse:
    """Parse → verify → pay on ALLOW (test rail). ESCALATE queues human approval."""
    try:
        payload = _orchestrator(request).execute_invoice(
            body.raw_text,
            use_llm_agent=body.use_llm_agent,
        )
    except ValueError as exc:
        decision = fail_closed_decision([str(exc)]).model_dump(mode="json")
        payload = {
            "parsed_invoice": None,
            "proposed_action": None,
            "source_mode": None,
            "decision": decision,
            "execution": {"status": "not_executed"},
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("unexpected error in /orchestrator/execute")
        decision = fail_closed_decision([exc]).model_dump(mode="json")
        payload = {
            "parsed_invoice": None,
            "proposed_action": None,
            "source_mode": None,
            "decision": decision,
            "execution": {"status": "not_executed"},
        }
    return JSONResponse(content=payload)


@router.post("/pay")
async def pay_proposal(body: PayProposalRequest, request: Request) -> JSONResponse:
    """Re-verify an agent's proposal and pay on ALLOW (test rail).

    External agents call this after ``POST /verify`` returns allow, or to queue
    human approval when the gate escalates. The gate always runs again here —
    proposals cannot bypass verification.
    """
    try:
        payload = _orchestrator(request).execute_proposal(
            body.proposed_action,
            body.source,
        )
    except ValueError as exc:
        decision = fail_closed_decision([str(exc)]).model_dump(mode="json")
        payload = {
            "parsed_invoice": body.source.get("invoice"),
            "proposed_action": body.proposed_action,
            "source_mode": "caller" if "fetch" not in body.source else "system_of_record",
            "decision": decision,
            "execution": {"status": "not_executed"},
        }
    except Exception as exc:  # noqa: BLE001
        logger.exception("unexpected error in /orchestrator/pay")
        decision = fail_closed_decision([exc]).model_dump(mode="json")
        payload = {
            "parsed_invoice": body.source.get("invoice"),
            "proposed_action": body.proposed_action,
            "decision": decision,
            "execution": {"status": "not_executed"},
        }
    return JSONResponse(content=payload)


@router.post("/approvals/{approval_id}/decide")
async def decide_approval(
    approval_id: str, body: ApprovalRequest, request: Request
) -> JSONResponse:
    """Approve or reject a pending ESCALATE, executing payment only on approve."""
    try:
        payload = _orchestrator(request).approve_and_execute(
            approval_id, approved=body.approve
        )
    except ValueError as exc:
        decision = fail_closed_decision([str(exc)]).model_dump(mode="json")
        payload = {"decision": decision, "execution": {"status": "not_executed"}}
    except Exception as exc:  # noqa: BLE001
        logger.exception("unexpected error in approval decide")
        decision = fail_closed_decision([exc]).model_dump(mode="json")
        payload = {"decision": decision, "execution": {"status": "not_executed"}}
    return JSONResponse(content=payload)


@router.get("/audit")
async def list_audit(request: Request, limit: int = 20) -> JSONResponse:
    """Recent orchestrator audit events (demo / ops visibility)."""
    events = _orchestrator_store(request).list_audit(limit=min(limit, 100))
    return JSONResponse(content={"events": events})
