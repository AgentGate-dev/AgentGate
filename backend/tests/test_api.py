"""HTTP contract of the thin FastAPI service (PRD SS9, D35-D38).

The gate promise over HTTP: POST /verify always answers 200 with a valid
Decision — verified or fail-closed — never a 5xx, never a framework 422, never
an allow it cannot back. The wire rejects what nothing reads (extra="forbid",
no po in v1), money survives as exact Decimals in both directions (JSON numbers
decoded via parse_float=Decimal; every Decimal serialized as a JSON string),
/verify is read-only, and tracing is an observer that can neither change a
decision nor leak raw_text content.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agentgate.api.verify import MAX_BODY_BYTES
from agentgate.core.duplicate_store import DuplicateStore
from agentgate.core.schemas import (
    Invoice,
    LineItem,
    Money,
    ProposedAction,
    Source,
    TaxLine,
    VerifyRequest,
)
from agentgate.core.tracing import NoopTracer
from agentgate.main import create_app
from tests.conftest import DEFAULT_RAW_TEXT


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


def action_payload(**overrides) -> dict:
    payload = {
        "action_type": "approve_payment",
        "invoice_number": "INV-001",
        "amount": {"value": "1240.00", "currency": "USD"},
        "vendor": "Acme Corp",
        "adjustments": [],
        "agent_rationale": "Totals match.",
    }
    payload.update(overrides)
    return payload


def verify_body(*, invoice: dict | None = None, action: dict | None = None, **source_extra) -> dict:
    source: dict = {"invoice": invoice if invoice is not None else invoice_payload()}
    if "fetch" not in source_extra and "raw_text" not in source_extra:
        source["raw_text"] = DEFAULT_RAW_TEXT
    source.update(source_extra)
    return {
        "proposed_action": action if action is not None else action_payload(),
        "source": source,
    }


class RaisingTracer:
    """A tracer that fails like a down/misconfigured Langfuse would (D37)."""

    def record(self, **kwargs) -> None:  # noqa: ARG002
        raise RuntimeError("tracing exploded")

    def shutdown(self) -> None:
        raise RuntimeError("tracing exploded")


class CapturingTracer:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def record(self, **kwargs) -> None:
        self.records.append(kwargs)

    def shutdown(self) -> None:
        return None


@pytest.fixture()
def store():
    s = DuplicateStore(":memory:")
    yield s
    s.close()


@pytest.fixture()
def client(store):
    with TestClient(create_app(store=store, tracer=NoopTracer())) as c:
        yield c


def assert_boundary_fields(body: dict) -> None:
    """trace_id/latency_ms/timestamp are stamped at the boundary on EVERY
    response, fail-closed included (PRD SS9, D35)."""
    uuid.UUID(body["trace_id"])  # parseable uuid4
    assert isinstance(body["latency_ms"], int)
    assert body["latency_ms"] >= 0
    parsed = datetime.fromisoformat(body["timestamp"])
    assert parsed.tzinfo is not None  # aware UTC, not naive local time


# --- /health -------------------------------------------------------------------


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- the fail-closed HTTP contract (the slice gate) ----------------------------


def test_not_json_body_fails_closed_over_http(client):
    resp = client.post("/verify", content=b"this is not json")
    assert resp.status_code == 200  # never a 5xx, never a 422 (D35)
    body = resp.json()
    assert body["decision"] == "escalate"
    assert body["score"] is None
    assert body["checks"] == []
    assert body["proposed_action"] is None
    assert [r["check"] for r in body["reasons"]] == ["fail_closed"]
    assert "not valid JSON" in body["reasons"][0]["message"]
    assert_boundary_fields(body)


def test_schema_invalid_body_fails_closed_and_is_bounded(client):
    junk = "9" * 50_000
    resp = client.post(
        "/verify",
        json=verify_body(invoice=invoice_payload(total={"value": junk, "currency": "USD"})),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "escalate"
    assert body["score"] is None
    (reason,) = body["reasons"]
    assert reason["check"] == "fail_closed"
    # the junk value must not ride the error message into the response/traces
    assert "9" * 100 not in resp.text
    assert len(reason["message"]) < 1_500
    assert_boundary_fields(body)


def test_missing_invoice_fails_closed(client):
    resp = client.post("/verify", json={"proposed_action": action_payload(), "source": {}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "escalate"
    (reason,) = body["reasons"]
    assert reason["check"] == "fail_closed"
    assert "invoice" in reason["message"]


def test_unexpected_internal_error_fails_closed_not_5xx(client, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr("agentgate.api.verify.decide", boom)
    resp = client.post("/verify", json=verify_body())
    assert resp.status_code == 200  # the catch-all IS the contract (D35)
    body = resp.json()
    assert body["decision"] == "escalate"
    (reason,) = body["reasons"]
    assert reason["check"] == "fail_closed"
    assert "kaboom" in reason["message"]
    assert_boundary_fields(body)


def test_oversized_body_fails_closed_without_parsing(client):
    resp = client.post("/verify", content=b"x" * (MAX_BODY_BYTES + 1))
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "escalate"
    (reason,) = body["reasons"]
    assert reason["check"] == "fail_closed"
    assert "request body exceeds" in reason["message"]


# --- verified decisions over HTTP ----------------------------------------------


def test_clean_request_allows_over_http(client):
    resp = client.post("/verify", json=verify_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "allow"
    assert body["score"] == "1.00"  # a Decimal is a JSON STRING on the wire (D35)
    assert [c["name"] for c in body["checks"]] == [
        "action_type_supported",
        "invoice_number_match",
        "structural_arithmetic",
        "currency_match",
        "action_amount_matches_total",
        "vendor_match",
        "duplicate_check",
    ]
    assert all(c["passed"] for c in body["checks"])
    assert body["evidence_used"] == ["invoice:INV-001", "raw_text"]
    assert body["proposed_action"]["vendor"] == "Acme Corp"
    assert_boundary_fields(body)


def test_money_as_json_numbers_is_lossless(client):
    # A caller may send money as JSON numbers; the endpoint decodes the body
    # with parse_float=Decimal, so 1240.00 arrives as the exact Decimal of its
    # literal text — no float ever exists (D1/D35).
    raw = json.dumps(verify_body()).replace('"1240.00"', "1240.00")
    assert '"1240.00"' not in raw  # the body really carries numbers now
    resp = client.post("/verify", content=raw.encode())
    body = resp.json()
    assert body["decision"] == "allow"
    assert body["proposed_action"]["amount"]["value"] == "1240.00"


def test_tampered_total_blocks_over_http(client):
    resp = client.post(
        "/verify",
        json=verify_body(action=action_payload(amount={"value": "12400.00", "currency": "USD"})),
    )
    body = resp.json()
    assert body["decision"] == "block"
    (reason,) = body["reasons"]
    assert reason["check"] == "action_amount_matches_total"
    assert reason["block_type"] == "agent_fixable"
    assert reason["field_to_change"] == "proposed_action.amount"
    # exact-decimal money as strings in both directions (D1 end to end)
    assert reason["expected"]["value"] == "1240.00"
    assert reason["received"]["value"] == "12400.00"


# --- the wire rejects what nothing reads (D36) ----------------------------------


def test_po_evidence_is_rejected_not_ignored(client):
    resp = client.post(
        "/verify", json=verify_body(po={"po_number": "PO-77", "amount": "1240.00"})
    )
    body = resp.json()
    assert body["decision"] == "escalate"
    (reason,) = body["reasons"]
    assert reason["check"] == "fail_closed"
    assert "po" in reason["message"]


def test_misspelled_adjustments_key_cannot_flip_escalate_to_block(client):
    # THE trap this hardening exists for: a caller declares a withholding but
    # misspells the key. Silently dropped, adjustments defaults to [] and the
    # amount difference becomes an agent-fixable BLOCK whose fixer would "pay
    # the full total" — laundering the exact overpayment the caller declared
    # away. It must fail closed instead (PRD SS9, D36).
    action = action_payload(amount={"value": "1000.00", "currency": "USD"})
    del action["adjustments"]
    action["adjustmnets"] = [{"type": "withholding", "value": "-240.00"}]
    resp = client.post("/verify", json=verify_body(action=action))
    body = resp.json()
    assert body["decision"] != "block"
    assert body["decision"] == "escalate"
    (reason,) = body["reasons"]
    assert reason["check"] == "fail_closed"
    assert "adjustmnets" in reason["message"]


@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (Money, {"value": "1.00", "currency": "USD", "rounding": "up"}),
        (
            LineItem,
            {
                "description": "d",
                "quantity": "1",
                "unit_price": {"value": "1.00", "currency": "USD"},
                "amount": {"value": "1.00", "currency": "USD"},
                "kind": "charge",
                "sku": "X-1",
            },
        ),
        (
            TaxLine,
            {"rate": "0.05", "amount": {"value": "1.00", "currency": "USD"}, "region": "CA"},
        ),
        (Invoice, invoice_payload(po_number="PO-77")),
        (ProposedAction, action_payload(priority="high")),
        (Source, {"invoice": invoice_payload(), "po": {}}),
        (VerifyRequest, {"proposed_action": action_payload(), "source": {"invoice": invoice_payload()}, "policy": {}}),
    ],
)
def test_wire_models_reject_unknown_fields(model, payload):
    with pytest.raises(ValidationError):
        model.model_validate(payload)


def test_line_item_and_tax_line_counts_are_bounded():
    item = {
        "description": "d",
        "quantity": "1",
        "unit_price": {"value": "1.00", "currency": "USD"},
        "amount": {"value": "1.00", "currency": "USD"},
        "kind": "charge",
    }
    assert Invoice.model_validate(invoice_payload(line_items=[dict(item)] * 500))
    with pytest.raises(ValidationError):
        Invoice.model_validate(invoice_payload(line_items=[dict(item)] * 501))
    tax = {"rate": "0.05", "amount": {"value": "1.00", "currency": "USD"}}
    assert Invoice.model_validate(invoice_payload(tax_lines=[dict(tax)] * 50))
    with pytest.raises(ValidationError):
        Invoice.model_validate(invoice_payload(tax_lines=[dict(tax)] * 51))


def test_raw_text_over_bound_fails_closed(client):
    resp = client.post("/verify", json=verify_body(raw_text="x" * 50_001))
    body = resp.json()
    assert body["decision"] == "escalate"
    (reason,) = body["reasons"]
    assert reason["check"] == "fail_closed"
    assert "raw_text" in reason["message"]


def test_quotation_raw_text_fails_closed(client):
    resp = client.post(
        "/verify",
        json=verify_body(
            raw_text=(
                "QUOTATION\nQuote #: QUO-1\nQuote Date: 2026-07-21\n"
                "Total: $100.00"
            ),
        ),
    )
    body = resp.json()
    assert body["decision"] == "escalate"
    (reason,) = body["reasons"]
    assert reason["check"] == "fail_closed"
    assert "invoices only" in reason["message"].lower()


def test_structured_invoice_without_raw_text_fails_closed(client):
    body = {
        "proposed_action": action_payload(),
        "source": {"invoice": invoice_payload()},
    }
    resp = client.post("/verify", json=body)
    payload = resp.json()
    assert payload["decision"] == "escalate"
    (reason,) = payload["reasons"]
    assert reason["check"] == "fail_closed"
    assert "raw_text" in reason["message"].lower()
    assert "forged json" in reason["message"].lower() or "agent-forged" in reason["message"].lower()


# --- raw_text -> grounding coverage over HTTP (D27 wiring) -----------------------


def test_raw_text_grounds_coverage_over_http(client):
    resp = client.post(
        "/verify",
        json=verify_body(raw_text="Invoice INV-001 from Acme Corp. Total Due: $1,240.00"),
    )
    body = resp.json()
    assert body["decision"] == "allow"
    assert body["score"] == "1.00"
    assert body["evidence_used"] == ["invoice:INV-001", "raw_text"]


def test_ungrounded_total_escalates_over_http(client):
    resp = client.post(
        "/verify", json=verify_body(raw_text="Totally different document. Total Due: $999.99")
    )
    body = resp.json()
    assert body["decision"] == "escalate"
    assert any(r["check"] == "total_not_grounded" for r in body["reasons"])


def test_empty_raw_text_is_rejected_at_the_boundary(client):
    resp = client.post("/verify", json=verify_body(raw_text="   "))
    body = resp.json()
    assert body["decision"] == "escalate"
    (reason,) = body["reasons"]
    assert reason["check"] == "fail_closed"
    assert "raw_text" in reason["message"].lower()


# --- /verify is read-only (D38) --------------------------------------------------


def test_verify_is_read_only_no_store_write_on_allow(client, store):
    first = client.post("/verify", json=verify_body()).json()
    second = client.post("/verify", json=verify_body()).json()
    assert first["decision"] == "allow"
    assert second["decision"] == "allow"  # a dry-run never burns the invoice number
    assert store.is_approved("INV-001") is False


def test_duplicate_store_read_is_wired(client, store):
    store.mark_approved("INV-001")
    resp = client.post("/verify", json=verify_body())
    body = resp.json()
    assert body["decision"] == "escalate"
    assert any(r["check"] == "duplicate_check" for r in body["reasons"])


# --- tracing: an observer, never a gate (D37) ------------------------------------


def test_tracing_failure_never_affects_the_decision(store):
    with TestClient(create_app(store=store, tracer=RaisingTracer())) as client:
        resp = client.post("/verify", json=verify_body())
        assert resp.status_code == 200
        assert resp.json()["decision"] == "allow"


def test_trace_records_raw_text_length_never_content(store):
    tracer = CapturingTracer()
    raw_text = "TOP-SECRET-INVOICE-TEXT Total Due: $1,240.00"
    with TestClient(create_app(store=store, tracer=tracer)) as client:
        resp = client.post("/verify", json=verify_body(raw_text=raw_text))
    body = resp.json()
    assert body["decision"] == "allow"
    (record,) = tracer.records
    assert record["trace_id"] == body["trace_id"]
    dumped = json.dumps(record["input"]) + json.dumps(record["output"])
    assert "TOP-SECRET-INVOICE-TEXT" not in dumped
    assert record["input"]["raw_text_length"] == len(raw_text)


def test_fail_closed_trace_records_no_body_content(store):
    tracer = CapturingTracer()
    with TestClient(create_app(store=store, tracer=tracer)) as client:
        client.post("/verify", content=b"SECRET-GARBAGE that is not json")
    (record,) = tracer.records
    assert "SECRET-GARBAGE" not in json.dumps(record["input"])
    assert record["input"]["body_bytes"] == len(b"SECRET-GARBAGE that is not json")


# --- CORS: explicit origins only, off by default (D40) ---------------------------


DASHBOARD_ORIGIN = "http://127.0.0.1:3987"


def test_cors_preflight_allowed_for_configured_origin(store):
    app = create_app(store=store, tracer=NoopTracer(), cors_origins=[DASHBOARD_ORIGIN])
    with TestClient(app) as client:
        resp = client.options(
            "/verify",
            headers={
                "Origin": DASHBOARD_ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == DASHBOARD_ORIGIN
        # narrowest grant: no credentials ever (D40)
        assert "access-control-allow-credentials" not in resp.headers


def test_cors_denies_unlisted_origin_and_absent_config(store):
    app = create_app(store=store, tracer=NoopTracer(), cors_origins=[DASHBOARD_ORIGIN])
    with TestClient(app) as client:
        resp = client.post(
            "/verify", json=verify_body(), headers={"Origin": "https://evil.example"}
        )
        assert "access-control-allow-origin" not in resp.headers
    # unconfigured (explicit empty) => the middleware is absent entirely
    bare = create_app(store=store, tracer=NoopTracer(), cors_origins=[])
    with TestClient(bare) as client:
        resp = client.post(
            "/verify", json=verify_body(), headers={"Origin": DASHBOARD_ORIGIN}
        )
        assert resp.status_code == 200
        assert "access-control-allow-origin" not in resp.headers


def test_cors_origins_parsed_from_env(store, monkeypatch):
    monkeypatch.setenv(
        "AGENTGATE_CORS_ORIGINS", f"https://agentgate.vercel.app, {DASHBOARD_ORIGIN}"
    )
    app = create_app(store=store, tracer=NoopTracer())
    with TestClient(app) as client:
        resp = client.options(
            "/verify",
            headers={
                "Origin": DASHBOARD_ORIGIN,
                "Access-Control-Request-Method": "POST",
            },
        )
        assert resp.headers["access-control-allow-origin"] == DASHBOARD_ORIGIN


# --- Fetch mode: independent source fetch (PRD SS0/SS9 Slice 9, D45/D46) --------

RECORD_RAW_TEXT = (
    "Acme Corp — Invoice INV-001. Widget: $1,240.00. Total due: $1,240.00 USD."
)


def write_record(directory, filename="record.json", *, invoice=None, raw_text=RECORD_RAW_TEXT):
    record = {"invoice": invoice if invoice is not None else invoice_payload()}
    if raw_text is not None:
        record["raw_text"] = raw_text
    (directory / filename).write_text(json.dumps(record), encoding="utf-8")


def fetch_body(*, action: dict | None = None, fetch: str = "INV-001") -> dict:
    return {
        "proposed_action": action if action is not None else action_payload(),
        "source": {"fetch": fetch},
    }


@pytest.fixture()
def fetch_client(store, tmp_path):
    from agentgate.core.system_of_record import DirectorySourceOfRecord

    write_record(tmp_path)
    app = create_app(
        store=store,
        tracer=NoopTracer(),
        source_of_record=DirectorySourceOfRecord(tmp_path),
    )
    with TestClient(app) as c:
        yield c


def test_fetch_mode_allows_against_the_stored_record_with_provenance(fetch_client):
    resp = fetch_client.post("/verify", json=fetch_body())
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "allow"
    assert body["score"] == "1.00"
    # Provenance on the wire (D45): the evidence came from the system of
    # record, not the caller — every entry says so.
    assert body["evidence_used"] == [
        "system_of_record:invoice:INV-001",
        "system_of_record:raw_text",
    ]
    assert len(body["checks"]) == 7
    assert_boundary_fields(body)


def test_fetch_mode_blocks_a_tampered_action_against_the_fetched_truth(fetch_client):
    # The trust-anchor property: the caller supplied only an identifier, so the
    # expected value in the block reason can only have come from the store.
    action = action_payload(amount={"value": "12400.00", "currency": "USD"})
    resp = fetch_client.post("/verify", json=fetch_body(action=action))
    body = resp.json()
    assert body["decision"] == "block"
    (reason,) = body["reasons"]
    assert reason["check"] == "action_amount_matches_total"
    assert reason["block_type"] == "agent_fixable"
    assert reason["expected"]["value"] == "1240.00"
    assert body["evidence_used"][0].startswith("system_of_record:")


def test_fetch_mode_unknown_invoice_fails_closed(fetch_client):
    resp = fetch_client.post("/verify", json=fetch_body(fetch="INV-999"))
    body = resp.json()
    assert body["decision"] == "escalate"
    assert body["score"] is None
    (reason,) = body["reasons"]
    assert reason["check"] == "fail_closed"
    assert "not found" in reason["message"]
    assert reason["block_type"] is None  # never agent-fixable (D31/D45)
    assert_boundary_fields(body)


def test_fetch_mode_without_a_configured_store_fails_closed(store, monkeypatch):
    monkeypatch.delenv("AGENTGATE_RECORDS_DIR", raising=False)
    with TestClient(create_app(store=store, tracer=NoopTracer())) as client:
        resp = client.post("/verify", json=fetch_body())
        body = resp.json()
        assert body["decision"] == "escalate"
        (reason,) = body["reasons"]
        assert reason["check"] == "fail_closed"
        assert "no system of record" in reason["message"]


def test_fetch_mixed_with_caller_evidence_fails_closed(fetch_client):
    body_json = fetch_body()
    body_json["source"]["invoice"] = invoice_payload()
    resp = fetch_client.post("/verify", json=body_json)
    body = resp.json()
    assert body["decision"] == "escalate"
    (reason,) = body["reasons"]
    assert reason["check"] == "fail_closed"
    assert "fetch" in reason["message"]


def test_fetch_mode_frame_check_catches_a_mismatched_action(fetch_client):
    # The frame stage stays live in fetch mode: an action naming a different
    # invoice than the fetched record escalates via invoice_number_match (D45).
    action = action_payload(invoice_number="INV-002")
    resp = fetch_client.post("/verify", json=fetch_body(action=action))
    body = resp.json()
    assert body["decision"] == "escalate"
    assert body["score"] is None
    mismatch = [c for c in body["checks"] if c["name"] == "invoice_number_match"]
    assert mismatch and not mismatch[0]["passed"]


def test_fetch_mode_duplicate_read_keys_on_the_fetched_number(store, tmp_path):
    from agentgate.core.system_of_record import DirectorySourceOfRecord

    write_record(tmp_path)
    store.mark_approved("INV-001")
    app = create_app(
        store=store,
        tracer=NoopTracer(),
        source_of_record=DirectorySourceOfRecord(tmp_path),
    )
    with TestClient(app) as client:
        body = client.post("/verify", json=fetch_body()).json()
        assert body["decision"] == "escalate"
        assert any(r["check"] == "duplicate_check" for r in body["reasons"])


def test_caller_mode_evidence_stays_unprefixed(client):
    # Mutation guard: an unconditional provenance prefix would claim fetched
    # evidence for caller-supplied input — the exact lie D45 forbids.
    body = client.post("/verify", json=verify_body()).json()
    assert body["decision"] == "allow"
    assert body["evidence_used"] == ["invoice:INV-001", "raw_text"]


def test_invoice_referencing_a_quotation_still_verifies(client):
    # Real invoices routinely cite their originating quotation; only quote-marked
    # text with NO invoice marker is rejected (PRD SS5b invoice-marker precedence).
    resp = client.post(
        "/verify",
        json=verify_body(
            raw_text=(
                "INVOICE\nInvoice #: INV-001\nAcme Corp\n"
                "As per quotation Q-88.\nTotal Due: $1,240.00"
            ),
        ),
    )
    assert resp.json()["decision"] == "allow"
