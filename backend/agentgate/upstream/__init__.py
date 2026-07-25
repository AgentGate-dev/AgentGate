"""Upstream agent layer — invoice ingestion and payment proposals.

AgentGate's core is ALLOW / BLOCK / ESCALATE only (``verify_action`` / ``decide()``).
This package holds the deterministic upstream work an MCP-speaking agent would do
before calling the gate: parse invoice text, propose a payment, assemble the
``VerifyRequest`` envelope.
"""

from .invoice_text import QUOTATION_REJECTION_MESSAGE, parse_invoice_text
from .pipeline import default_proposed_action, process_invoice

__all__ = [
    "QUOTATION_REJECTION_MESSAGE",
    "default_proposed_action",
    "parse_invoice_text",
    "process_invoice",
]
