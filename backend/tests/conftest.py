"""Shared test helpers. The router is the only SUBSTITUTION seam (DECISIONS D9):
tests inject stubbed raw LLM outputs and run the real parsing/grounding/decision
logic unmocked; fault-injection doubles (a raising decide, a raising tracer)
exercise otherwise-unreachable failure paths and never return canned verdicts."""

from __future__ import annotations

from pathlib import Path

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "sample_invoices"

# Grounded invoice text shared by HTTP/MCP contract tests (matches invoice_payload totals).
DEFAULT_RAW_TEXT = "Invoice INV-001 from Acme Corp. Total Due: $1,240.00"


def load_sample(name: str) -> str:
    return (SAMPLES / name).read_text()
