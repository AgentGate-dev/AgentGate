"""Independent source fetch — the trust-anchor upgrade (PRD SS0/SS9, D45/D46).

Fetch mode removes the caller from the evidence path: the agent names WHICH
invoice, the operator-configured system of record says what it contains. The
contract under test: the Source union (caller XOR fetch, mixing rejected), the
directory store (keyed by record content, never filename; caller-grade schema
validation on operator data; any corrupt/ambiguous entry fails the fetch), and
resolve_source (pass-through in caller mode, typed SourceOfRecordError on every
fetch failure — the boundary converts it to a fail-closed escalate, D11/D45).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentgate.core.schemas import Source
from agentgate.core.system_of_record import (
    MAX_RECORD_BYTES,
    DirectorySourceOfRecord,
    SourceOfRecordError,
    build_source_of_record,
    resolve_source,
    system_of_record_evidence,
)


RAW_TEXT = "Acme Corp — Invoice INV-001. Widget: $1,240.00. Total due: $1,240.00 USD."


def invoice_payload(**overrides) -> dict:
    payload = {
        "invoice_number": "INV-001",
        "vendor": "Acme Corp",
        "date": "2026-01-15",
        "currency": "USD",
        "line_items": [
            {
                "description": "Widget",
                "quantity": "1",
                "unit_price": {"value": "1240.00", "currency": "USD"},
                "amount": {"value": "1240.00", "currency": "USD"},
                "kind": "charge",
            }
        ],
        "tax_lines": [],
        "total": {"value": "1240.00", "currency": "USD"},
    }
    payload.update(overrides)
    return payload


def write_record(
    directory: Path,
    filename: str = "record.json",
    *,
    invoice: dict | None = None,
    raw_text: str | None = RAW_TEXT,
) -> Path:
    record: dict = {"invoice": invoice if invoice is not None else invoice_payload()}
    if raw_text is not None:
        record["raw_text"] = raw_text
    path = directory / filename
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_non_regular_record_entry_fails_the_fetch(tmp_path):
    # A directory (or FIFO/socket) named *.json is not a record; it must fail the
    # fetch closed, never be read (a FIFO read would hang the event loop).
    write_record(tmp_path, "real.json")  # a valid INV-001 record exists alongside
    (tmp_path / "trap.json").mkdir()
    with pytest.raises(SourceOfRecordError, match="not a regular file"):
        DirectorySourceOfRecord(tmp_path).fetch("INV-001")


def test_oversized_record_file_is_capped(tmp_path):
    big = tmp_path / "big.json"
    big.write_text(" " * (MAX_RECORD_BYTES + 10) + "{}", encoding="utf-8")
    with pytest.raises(SourceOfRecordError):
        DirectorySourceOfRecord(tmp_path).fetch("INV-001")


# --- Source: the caller-XOR-fetch union (D45) -----------------------------------


def test_fetch_alone_validates_and_strips_whitespace():
    source = Source(fetch="  INV-001  ")
    assert source.fetch == "INV-001"
    assert source.invoice is None


def test_fetch_combined_with_invoice_is_rejected():
    with pytest.raises(ValidationError, match="fetch"):
        Source.model_validate({"fetch": "INV-001", "invoice": invoice_payload()})


def test_fetch_combined_with_raw_text_is_rejected():
    # Caller raw_text riding along a fetched invoice would make the
    # system_of_record provenance label a lie (D45).
    with pytest.raises(ValidationError, match="fetch"):
        Source.model_validate({"fetch": "INV-001", "raw_text": "Total $1,240.00"})


def test_caller_invoice_without_raw_text_is_rejected():
    with pytest.raises(ValidationError, match="raw_text"):
        Source.model_validate({"invoice": invoice_payload()})


def test_source_with_neither_mode_is_rejected():
    with pytest.raises(ValidationError, match="invoice"):
        Source.model_validate({})


def test_fetch_gets_identifier_bounds():
    with pytest.raises(ValidationError):
        Source.model_validate({"fetch": "   "})
    with pytest.raises(ValidationError, match="too long"):
        Source.model_validate({"fetch": "X" * 101})


# --- DirectorySourceOfRecord (D46) ----------------------------------------------


def test_fetch_keys_on_record_content_never_filename(tmp_path):
    write_record(tmp_path, "zzz-not-the-invoice-number.json")
    store = DirectorySourceOfRecord(tmp_path)
    record = store.fetch("INV-001")
    assert record.invoice is not None
    assert record.invoice.invoice_number == "INV-001"
    assert record.raw_text == RAW_TEXT
    with pytest.raises(SourceOfRecordError, match="not found"):
        store.fetch("zzz-not-the-invoice-number")


def test_unknown_invoice_raises_not_found(tmp_path):
    write_record(tmp_path)
    with pytest.raises(SourceOfRecordError, match="not found"):
        DirectorySourceOfRecord(tmp_path).fetch("INV-999")


def test_traversal_shaped_identifier_is_just_a_key_that_matches_nothing(tmp_path):
    # No path is ever constructed from the identifier (D46) — a hostile-looking
    # key is inert: plain not-found, no filesystem access outside the directory.
    write_record(tmp_path)
    with pytest.raises(SourceOfRecordError, match="not found"):
        DirectorySourceOfRecord(tmp_path).fetch("../../etc/passwd")


def test_missing_directory_raises(tmp_path):
    with pytest.raises(SourceOfRecordError, match="directory"):
        DirectorySourceOfRecord(tmp_path / "nope").fetch("INV-001")


def test_corrupt_json_file_fails_the_fetch_even_with_a_valid_match_present(tmp_path):
    # Fail-closed: the broken file might be the requested record, and a system
    # of record with corrupt entries cannot anchor trust (D46).
    write_record(tmp_path, "good.json")
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SourceOfRecordError, match="broken.json"):
        DirectorySourceOfRecord(tmp_path).fetch("INV-001")


def test_schema_invalid_record_fails_the_fetch(tmp_path):
    # Operator data gets caller-grade validation, extra="forbid" included: a
    # misspelled raw_text key silently dropped would flip grounding coverage
    # to 1.0 — the fail-open direction (D46).
    path = tmp_path / "typo.json"
    path.write_text(
        json.dumps({"invoice": invoice_payload(), "rawtext": "oops"}), encoding="utf-8"
    )
    with pytest.raises(SourceOfRecordError, match="typo.json"):
        DirectorySourceOfRecord(tmp_path).fetch("INV-001")


def test_record_that_is_itself_fetch_mode_is_invalid(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"fetch": "INV-002"}), encoding="utf-8")
    with pytest.raises(SourceOfRecordError, match="invoice"):
        DirectorySourceOfRecord(tmp_path).fetch("INV-001")


def test_oversized_record_fails_the_fetch(tmp_path):
    (tmp_path / "huge.json").write_bytes(b"x" * (MAX_RECORD_BYTES + 1))
    with pytest.raises(SourceOfRecordError, match="huge.json"):
        DirectorySourceOfRecord(tmp_path).fetch("INV-001")


def test_two_records_claiming_the_requested_number_is_ambiguous(tmp_path):
    write_record(tmp_path, "a.json")
    write_record(tmp_path, "b.json")
    with pytest.raises(SourceOfRecordError, match="ambiguous"):
        DirectorySourceOfRecord(tmp_path).fetch("INV-001")


def test_stored_json_numbers_stay_exact_decimals(tmp_path):
    # D1 applies to disk: a stored numeric 1240.00 must be the exact Decimal of
    # its literal text (parse_float=Decimal), same as over HTTP (D35).
    record = {
        "invoice": invoice_payload(
            line_items=[], total={"value": 1240.00, "currency": "USD"}
        ),
        "raw_text": "Invoice INV-001. Total due: $1,240.00 USD.",
    }
    (tmp_path / "numeric.json").write_text(json.dumps(record), encoding="utf-8")
    fetched = DirectorySourceOfRecord(tmp_path).fetch("INV-001")
    assert fetched.invoice is not None
    assert fetched.invoice.total.value == Decimal("1240.00")


# --- build_source_of_record / resolve_source (D45) ------------------------------


def test_build_source_of_record_unset_env_means_none():
    assert build_source_of_record({}) is None
    assert build_source_of_record({"AGENTGATE_RECORDS_DIR": "  "}) is None


def test_build_source_of_record_wires_the_directory(tmp_path):
    store = build_source_of_record({"AGENTGATE_RECORDS_DIR": str(tmp_path)})
    assert isinstance(store, DirectorySourceOfRecord)


def test_resolve_source_caller_mode_passes_through_unchanged():
    source = Source.model_validate({"invoice": invoice_payload(), "raw_text": RAW_TEXT})
    resolved = resolve_source(source, None)
    assert resolved.fetched is False
    assert resolved.invoice is source.invoice
    assert resolved.raw_text == RAW_TEXT


def test_resolve_source_fetch_mode_returns_the_stored_record(tmp_path):
    write_record(tmp_path)
    resolved = resolve_source(
        Source(fetch="INV-001"), DirectorySourceOfRecord(tmp_path)
    )
    assert resolved.fetched is True
    assert resolved.invoice.invoice_number == "INV-001"
    assert resolved.raw_text == RAW_TEXT


def test_resolve_source_without_a_configured_store_raises(tmp_path):
    with pytest.raises(SourceOfRecordError, match="AGENTGATE_RECORDS_DIR"):
        resolve_source(Source(fetch="INV-001"), None)


def test_evidence_prefix_marks_provenance():
    assert system_of_record_evidence(["invoice:INV-001", "raw_text"]) == [
        "system_of_record:invoice:INV-001",
        "system_of_record:raw_text",
    ]


def test_invoice_marked_raw_text_mentioning_a_quote_is_accepted():
    # A legitimate invoice routinely references its originating quotation
    # ("as per quotation Q-123"). Only quote-marked text with NO invoice
    # marker is rejected (PRD SS5b invoice-marker precedence).
    source = Source.model_validate(
        {
            "invoice": invoice_payload(),
            "raw_text": (
                "INVOICE\nInvoice #: INV-001\nAs per quotation Q-123.\n"
                "Total Due: $1,240.00"
            ),
        }
    )
    assert source.raw_text is not None


def test_quote_marked_raw_text_without_invoice_marker_is_rejected():
    with pytest.raises(ValidationError, match="invoices only"):
        Source.model_validate(
            {
                "invoice": invoice_payload(),
                "raw_text": "QUOTATION\nQuote #: Q-1\nTotal: $1,240.00",
            }
        )
