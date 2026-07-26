"""POST /verify — the HTTP boundary of the gate (PRD SS9, D35-D38).

The endpoint owns its body handling end to end and always answers HTTP 200
with a Decision — verified or fail-closed — never a 5xx, never a framework
422, never an allow it cannot back. Framework body parsing is bypassed on
purpose (D35): it would 422 before our fail-closed contract could run, and it
floats every JSON number, which the Money validator rightly rejects —
``json.loads(..., parse_float=Decimal)`` keeps a numeric ``1240.00`` the exact
Decimal of its literal text (D1).

/verify is read-only (D38): it reads the duplicate store at decision time and
writes nothing.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from agentgate.core.decision import decide, fail_closed_decision
from agentgate.core.rate_limit import client_key
from agentgate.core.schemas import VerifyRequest
from agentgate.core.system_of_record import (
    SourceOfRecordError,
    resolve_source,
    system_of_record_evidence,
)
from agentgate.core.tracing import record_safely

logger = logging.getLogger("agentgate.api")

router = APIRouter()

# The envelope cap (PRD SS9, D36): schema bounds cap fields; this caps the
# body itself. Sized so no schema-valid request approaches it (worst case is
# ~0.5 MiB) — it can only ever reject a flood, never a valid body.
MAX_BODY_BYTES = 1_048_576


class BodyTooLargeError(ValueError):
    """Request body over MAX_BODY_BYTES — refused before parsing (D36)."""

    def __init__(self, received: int) -> None:
        super().__init__(
            f"request body exceeds {MAX_BODY_BYTES} bytes "
            f"(received at least {received}); refusing to parse it."
        )


async def _read_body(request: Request) -> bytes:
    """Read the body, enforcing the cap on the declared Content-Length first
    and again while streaming — a chunked or lying sender is still capped."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise BodyTooLargeError(int(declared))
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_BODY_BYTES:
            raise BodyTooLargeError(received)
        chunks.append(chunk)
    return b"".join(chunks)


def _unvalidated(body_bytes: int) -> dict:
    """Trace input for the fail-closed path: the body length only, never the
    raw body — nothing was validated, so nothing may be echoed (D37/D34)."""
    return {"validated": False, "body_bytes": body_bytes}


@router.post("/verify")
async def verify(request: Request) -> JSONResponse:
    # Per-IP rate limit (Slice 13, D56): exposure hygiene for an invited-attack
    # surface, not auth. A 429 is transport-level like 404/405 (D35) — no
    # Decision body is owed to a request the limiter refused.
    limiter = getattr(request.app.state, "rate_limiter", None)
    if limiter is not None and not limiter.allow(
        client_key(dict(request.headers), request.client.host if request.client else "unknown")
    ):
        return JSONResponse(
            status_code=429,
            content={"detail": "rate limit exceeded; retry shortly."},
        )

    started = time.perf_counter()
    body_bytes = 0
    try:
        body = await _read_body(request)
        body_bytes = len(body)
        payload = json.loads(body, parse_float=Decimal)
        req = VerifyRequest.model_validate(payload)
        resolved = resolve_source(req.source, request.app.state.source_of_record)
        raw_text = resolved.raw_text
        is_duplicate = request.app.state.store.is_approved(
            resolved.invoice.invoice_number
        )
        decision = decide(
            resolved.invoice,
            req.proposed_action,
            policy=request.app.state.policy,
            raw_text=raw_text,
            is_duplicate=is_duplicate,
        )
        if resolved.fetched:
            # Provenance is a boundary fact (D45): decide() cannot know where
            # its invoice came from, so the prefix is stamped here.
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
        # An expected operational failure (not found, unconfigured store,
        # corrupt record) — fail closed without the catch-all's stack trace.
        decision = fail_closed_decision([str(exc)])
        trace_input = _unvalidated(body_bytes)
    except BodyTooLargeError as exc:
        decision = fail_closed_decision([str(exc)])
        trace_input = _unvalidated(body_bytes)
    except ValidationError as exc:
        decision = fail_closed_decision([exc])
        trace_input = _unvalidated(body_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        decision = fail_closed_decision([f"request body is not valid JSON: {exc}"])
        trace_input = _unvalidated(body_bytes)
    except Exception as exc:  # noqa: BLE001 — the catch-all IS the contract (D35):
        # an exception class nobody anticipated must degrade to "cannot verify
        # -> escalate", never to a 5xx. Logged: fail-closed is not fail-silent.
        logger.exception("unexpected error while verifying; failing closed")
        decision = fail_closed_decision([exc])
        trace_input = _unvalidated(body_bytes)

    decision = decision.model_copy(
        update={
            "trace_id": str(uuid.uuid4()),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    response_payload = decision.model_dump(mode="json")
    record_safely(
        request.app.state.tracer,
        trace_id=decision.trace_id,
        name="verify",
        input=trace_input,
        output=response_payload,
    )
    return JSONResponse(content=response_payload)
