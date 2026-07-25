"""Run the verification gate with injected app dependencies (shared store)."""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import ValidationError

from agentgate.core.decision import decide, fail_closed_decision
from agentgate.core.duplicate_store import DuplicateStore
from agentgate.core.policy import Policy
from agentgate.core.schemas import Decision, VerifyRequest
from agentgate.core.system_of_record import (
    SourceOfRecord,
    SourceOfRecordError,
    resolve_source,
    system_of_record_evidence,
)


def run_gate_decision(
    proposed_action: dict[str, Any],
    source: dict[str, Any],
    *,
    store: DuplicateStore,
    policy: Policy,
    source_of_record: Optional[SourceOfRecord],
) -> dict[str, Any]:
    """Verify a proposal using the same path as ``POST /verify`` (read-only gate)."""
    started = time.perf_counter()
    try:
        req = VerifyRequest.model_validate(
            {"proposed_action": proposed_action, "source": source}
        )
        resolved = resolve_source(req.source, source_of_record)
        raw_text = resolved.raw_text
        is_duplicate = store.is_approved(resolved.invoice.invoice_number)
        decision = decide(
            resolved.invoice,
            req.proposed_action,
            policy=policy,
            raw_text=raw_text,
            is_duplicate=is_duplicate,
        )
        if resolved.fetched:
            decision = decision.model_copy(
                update={"evidence_used": system_of_record_evidence(decision.evidence_used)}
            )
    except SourceOfRecordError as exc:
        decision = fail_closed_decision([str(exc)])
    except ValidationError as exc:
        decision = fail_closed_decision([exc])
    except Exception as exc:  # noqa: BLE001
        decision = fail_closed_decision([exc])

    decision = decision.model_copy(
        update={
            "trace_id": str(uuid.uuid4()),
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    return Decision.model_validate(decision).model_dump(mode="json")
