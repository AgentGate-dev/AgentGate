"""Tests for upstream invoice parsing and agent pipeline.

The parser's contract (PRD §5b): understand every common money document —
parse payable ones (invoices/bills) across layouts, labels, currencies, and
number formats; recognize non-payable ones (quotations, receipts, purchase
orders, credit notes, statements) and reject each with an instructive,
type-specific message. Anything else fails with a message naming what was
looked for — never a crash, never a silent misparse.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentgate.main import create_app
from agentgate.upstream.invoice_text import parse_invoice_text
from agentgate.upstream.pipeline import process_invoice

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "sample_invoices"


def test_parse_acme_sample():
    text = (SAMPLES / "acme_good.txt").read_text()
    parsed = parse_invoice_text(text)
    assert parsed.invoice_number == "INV-001"
    assert parsed.total.value == "1240.00"
    assert parsed.vendor == "Acme Corp"


def test_process_invoice_allows_acme():
    text = (SAMPLES / "acme_good.txt").read_text()
    result = process_invoice(text)
    assert result["decision"]["decision"] == "allow"
    assert result["proposed_action"]["amount"]["value"] == "1240.00"


def test_agent_process_endpoint():
    app = create_app(cors_origins=["http://127.0.0.1:3000"])
    client = TestClient(app)
    text = (SAMPLES / "acme_good.txt").read_text()
    resp = client.post("/agent/process", json={"raw_text": text})
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"]["decision"] == "allow"


def test_agent_process_rejects_quotation():
    app = create_app()
    client = TestClient(app)
    resp = client.post(
        "/agent/process",
        json={"raw_text": "QUOTATION\nQuote #: Q-1\nTotal: $100.00"},
    )
    assert resp.status_code == 200
    assert resp.json()["decision"]["decision"] == "escalate"


# --- document-type understanding: payable layouts, labels, formats ---------------

EURO_INVOICE = """NORDWIND GMBH
Musterstrasse 12, 10115 Berlin

INVOICE

Invoice No.: RE-2026-118
Invoice Date: 14 July 2026

Bill To:
  Widget Buyers LLC

--------------------------------------------------------------
Description                 Qty    Unit Price        Amount
--------------------------------------------------------------
Consulting services          10      100,00        1.000,00
Software licence              2      120,25          240,50
--------------------------------------------------------------
                                     Subtotal:      1.240,50
                                     VAT (0%):          0,00
                                     Amount Due: EUR 1.240,50
"""


def test_european_invoice_parses_and_allows():
    parsed = parse_invoice_text(EURO_INVOICE)
    assert parsed.invoice_number == "RE-2026-118"
    assert parsed.currency == "EUR"
    assert parsed.date == "2026-07-14"
    assert parsed.total.value == "1240.50"
    assert [li.amount.value for li in parsed.line_items] == ["1000.00", "240.50"]
    result = process_invoice(EURO_INVOICE)
    assert result["decision"]["decision"] == "allow"


TAXED_INVOICE = """BRIGHT OFFICE SUPPLIES LTD
14 Market Lane, Manchester

INVOICE

Invoice Number: BOS-4471
Issue Date: 2026-06-30

--------------------------------------------------------------
Description                 Qty    Unit Price        Amount
--------------------------------------------------------------
Standing desk                 2      £240.00        £480.00
Office chair                  1      £120.00        £120.00
--------------------------------------------------------------
                                     Subtotal:      £600.00
                                     VAT (20%):     £120.00
                                     Balance Due:   £720.00
"""


def test_taxed_invoice_parses_tax_lines_and_allows():
    parsed = parse_invoice_text(TAXED_INVOICE)
    assert parsed.currency == "GBP"
    assert parsed.total.value == "720.00"
    assert len(parsed.tax_lines) == 1
    assert parsed.tax_lines[0].amount.value == "120.00"
    assert parsed.tax_lines[0].rate == "0.2"
    result = process_invoice(TAXED_INVOICE)
    assert result["decision"]["decision"] == "allow", result["decision"]["reasons"]


DISCOUNT_INVOICE = """ACME CORP

INVOICE

Invoice #: INV-DISC-1
Issue Date: 2026-07-01

--------------------------------------------------------------
Description                 Qty    Unit Price        Amount
--------------------------------------------------------------
Widget A                      2      $100.00        $200.00
Loyalty discount               -           -        -$20.00
--------------------------------------------------------------
                                     Total Due:      $180.00
"""


def test_discount_row_is_typed_and_arithmetic_holds():
    parsed = parse_invoice_text(DISCOUNT_INVOICE)
    kinds = {li.description: li.kind for li in parsed.line_items}
    assert kinds["Loyalty discount"] == "discount"
    discount = next(li for li in parsed.line_items if li.kind == "discount")
    assert discount.amount.value == "20.00"
    result = process_invoice(DISCOUNT_INVOICE)
    assert result["decision"]["decision"] == "allow", result["decision"]["reasons"]


TOTAL_ONLY_INVOICE = """PARKSIDE CONSULTING

INVOICE

Invoice #: PC-889
Issue Date: 2026-07-10

For professional services rendered in June 2026.

Amount Due: $2,500.00
"""


def test_total_only_invoice_parses_and_gate_escalates_honestly():
    parsed = parse_invoice_text(TOTAL_ONLY_INVOICE)
    assert parsed.line_items == []
    assert parsed.total.value == "2500.00"
    result = process_invoice(TOTAL_ONLY_INVOICE)
    decision = result["decision"]
    assert decision["decision"] == "escalate"
    assert any(r["check"] == "structural_arithmetic" for r in decision["reasons"])


INDIAN_INVOICE = """TAJ SOFTWARE PVT LTD
Bengaluru

TAX INVOICE

Invoice No.: TS-2026-77
Invoice Date: 2026-07-01

--------------------------------------------------------------
Description                 Qty    Unit Price          Amount
--------------------------------------------------------------
Annual licence                1    ₹1,24,000.00    ₹1,24,000.00
--------------------------------------------------------------
                                Total Amount: ₹1,24,000.00
"""


def test_indian_grouping_and_rupee_symbol():
    # Any Indian-grouped amount is at least one lakh, which sits above the
    # default $10k policy ceiling — so the honest outcome is an escalate whose
    # ONLY reason is the policy threshold, with every verification check
    # passing (parse, arithmetic, grounding, currency all correct).
    parsed = parse_invoice_text(INDIAN_INVOICE)
    assert parsed.currency == "INR"
    assert parsed.total.value == "124000.00"
    result = process_invoice(INDIAN_INVOICE)
    decision = result["decision"]
    assert decision["decision"] == "escalate"
    assert all(c["passed"] for c in decision["checks"])
    assert [r["check"] for r in decision["reasons"]] == ["policy_amount_threshold"]


DECIMAL_QTY_INVOICE = """FIELD SERVICES CO

INVOICE

Invoice #: FS-330
Issue Date: 2026-07-02

--------------------------------------------------------------
Description                 Qty    Unit Price        Amount
--------------------------------------------------------------
Consulting hours            2.5      $100.00        $250.00
--------------------------------------------------------------
                                     Total Due:      $250.00
"""


def test_decimal_quantity_rows_parse():
    parsed = parse_invoice_text(DECIMAL_QTY_INVOICE)
    assert parsed.line_items[0].quantity == "2.5"
    result = process_invoice(DECIMAL_QTY_INVOICE)
    assert result["decision"]["decision"] == "allow", result["decision"]["reasons"]


WHOLE_DOLLAR_INVOICE = """PLAIN BILLING LLC

INVOICE

Invoice #: PB-12
Issue Date: 2026-07-03

--------------------------------------------------------------
Description                 Qty    Unit Price        Amount
--------------------------------------------------------------
Widget                        2         $100           $200
--------------------------------------------------------------
                                     Total Due:        $200
"""


def test_whole_dollar_amounts_parse():
    parsed = parse_invoice_text(WHOLE_DOLLAR_INVOICE)
    assert parsed.total.value == "200.00"
    result = process_invoice(WHOLE_DOLLAR_INVOICE)
    assert result["decision"]["decision"] == "allow", result["decision"]["reasons"]


INVOICE_REFERENCING_QUOTE = """ACME CORP

INVOICE

Invoice #: INV-900
Issue Date: 2026-07-01

As per quotation Q-2001 accepted on 2026-06-20.

--------------------------------------------------------------
Description                 Qty    Unit Price        Amount
--------------------------------------------------------------
Widget                        1       $50.00         $50.00
--------------------------------------------------------------
                                     Total Due:       $50.00
"""


def test_invoice_referencing_a_quotation_is_still_an_invoice():
    parsed = parse_invoice_text(INVOICE_REFERENCING_QUOTE)
    assert parsed.invoice_number == "INV-900"
    result = process_invoice(INVOICE_REFERENCING_QUOTE)
    assert result["decision"]["decision"] == "allow", result["decision"]["reasons"]


# --- document-type understanding: non-payable documents get typed rejections -----


def test_receipt_is_rejected_as_already_paid():
    text = (
        "PAYMENT RECEIPT\n"
        "Receipt #: R-1001\n"
        "Invoice #: INV-42\n"
        "Paid in full: $100.00\n"
    )
    with pytest.raises(ValueError, match="[Rr]eceipt"):
        parse_invoice_text(text)


def test_purchase_order_is_rejected_as_not_a_bill():
    text = "PURCHASE ORDER\nPO #: PO-777\nTotal: $9,000.00\n"
    with pytest.raises(ValueError, match="[Pp]urchase order"):
        parse_invoice_text(text)


def test_credit_note_is_rejected_as_out_of_scope():
    text = "CREDIT NOTE\nCredit Note #: CN-12\nInvoice #: INV-42\nTotal: $50.00\n"
    with pytest.raises(ValueError, match="[Cc]redit note"):
        parse_invoice_text(text)


def test_statement_is_rejected_as_a_summary():
    text = "STATEMENT OF ACCOUNT\nAccount: 123\nTotal Due: $500.00\n"
    with pytest.raises(ValueError, match="[Ss]tatement"):
        parse_invoice_text(text)


def test_negative_total_is_rejected_as_credit_note():
    text = (
        "REFUNDING VENDOR\n\nINVOICE\n\nInvoice #: NEG-1\nIssue Date: 2026-07-01\n\n"
        "Total Due: -$50.00\n"
    )
    with pytest.raises(ValueError, match="[Cc]redit"):
        parse_invoice_text(text)


def test_two_currencies_are_rejected_as_ambiguous():
    text = (
        "DUAL CURRENCY LTD\n\nINVOICE\n\nInvoice #: MIX-1\nIssue Date: 2026-07-01\n\n"
        "--------------------------------------------------------------\n"
        "Description                 Qty    Unit Price        Amount\n"
        "--------------------------------------------------------------\n"
        "Widget                        1      €100.00       €100.00\n"
        "--------------------------------------------------------------\n"
        "                                     Total Due:      $110.00\n"
    )
    with pytest.raises(ValueError, match="currenc"):
        parse_invoice_text(text)


def test_unparseable_document_names_what_was_looked_for():
    with pytest.raises(ValueError, match="[Ii]nvoice"):
        parse_invoice_text("Dear team,\nplease see the attached document.\nThanks.")


def test_oversized_raw_text_fails_closed_over_http():
    app = create_app()
    client = TestClient(app)
    text = (SAMPLES / "acme_good.txt").read_text() + ("x" * 60_000)
    resp = client.post("/agent/process", json={"raw_text": text})
    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"]["decision"] == "escalate"
    assert any("raw_text" in r["message"] for r in body["decision"]["reasons"])


def test_http_app_serves_without_the_optional_mcp_package():
    # The FastAPI surface must never require the optional MCP SDK: CI's e2e job
    # installs backend[server] only, and a transitive `import mcp` from the
    # upstream pipeline took the whole server down. Block `mcp` in a clean
    # subprocess, boot the app, and drive /agent/process end to end.
    import subprocess
    import sys

    code = (
        "import sys\n"
        "sys.modules['mcp'] = None\n"
        "from pathlib import Path\n"
        "from fastapi.testclient import TestClient\n"
        "from agentgate.main import create_app\n"
        "client = TestClient(create_app())\n"
        f"text = Path({str(SAMPLES / 'acme_good.txt')!r}).read_text()\n"
        "resp = client.post('/agent/process', json={'raw_text': text})\n"
        "assert resp.status_code == 200, resp.status_code\n"
        "body = resp.json()\n"
        "assert body['decision']['decision'] == 'allow', body['decision']\n"
        "print('ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
