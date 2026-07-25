"""Upstream MCP server — invoice ingestion and agent proposals.

The **core gate** is ``agentgate-mcp`` with a single tool: ``verify_action``
(ALLOW / BLOCK / ESCALATE only). This server holds everything *before* the gate:

  parse_invoice → propose_payment → (orchestrator calls verify_action)

Or use ``process_invoice`` for a one-shot path that calls ``verify_action``
in-process after parsing and proposing.

Run: ``agentgate-upstream-mcp`` or ``python -m agentgate.mcp.upstream_server``.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from agentgate.upstream.invoice_text import parse_invoice_text, parsed_invoice_to_wire
from agentgate.upstream.pipeline import (
    default_proposed_action_from_wire,
    process_fetch,
    process_invoice as run_process_invoice,
)

mcp = FastMCP("agentgate-upstream")


@mcp.tool()
def parse_invoice(raw_text: str) -> dict[str, Any]:
    """Parse plain-text invoice evidence into structured fields + raw_text.

    Upstream step before ``verify_action``. Deterministic — no LLM. Rejects
    quotations and estimates."""
    parsed = parse_invoice_text(raw_text)
    return {
        "invoice": parsed_invoice_to_wire(parsed),
        "raw_text": parsed.raw_text,
    }


@mcp.tool()
def propose_payment(invoice: dict) -> dict[str, Any]:
    """Build a default approve_payment proposal from a structured invoice dict.

    Upstream step before ``verify_action``. In production your agent may call an
    LLM here; this reference implementation proposes the invoice total."""
    return default_proposed_action_from_wire(invoice)


@mcp.tool()
def process_invoice(raw_text: str) -> dict[str, Any]:
    """One-shot upstream + gate: parse invoice text, propose payment, verify.

    Returns ``parsed_invoice``, ``proposed_action``, and the gate ``decision``
    (allow | block | escalate). For production, compose ``parse_invoice``,
    ``propose_payment``, and core ``verify_action`` in your orchestrator."""
    return run_process_invoice(raw_text)


@mcp.tool()
def process_fetch_payment(
    fetch: str,
    amount_value: str,
    vendor: str,
    currency: str = "USD",
) -> dict[str, Any]:
    """Propose and verify a payment against a system-of-record fetch identifier.

    Upstream supplies amount/vendor; the gate loads invoice truth from
    ``AGENTGATE_RECORDS_DIR``."""
    proposed_action = {
        "action_type": "approve_payment",
        "invoice_number": fetch.strip(),
        "amount": {"value": amount_value, "currency": currency},
        "vendor": vendor,
        "adjustments": [],
        "agent_rationale": "Agent proposes payment against a fetched invoice record.",
    }
    return process_fetch(fetch, proposed_action)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
