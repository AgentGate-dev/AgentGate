"""Deterministic plain-text money-document parser (upstream layer).

Mirrors ``frontend/lib/invoice-parser.ts`` so HTTP, MCP upstream, and the demo
agent share one parsing contract before the gate verifies. The contract
(PRD §5b): parse payable documents (invoices/bills) across layouts, labels,
currencies, and number formats; recognize non-payable money documents
(quotations, receipts, purchase orders, credit notes, statements) and reject
each with an instructive, type-specific message; reject ambiguity (two
currencies, negative totals) rather than guess. No LLM anywhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Literal, Optional

from agentgate.core.schemas import INVOICE_MARKER_RE

QUOTATION_REJECTION_MESSAGE = (
    "AgentGate verifies payments against invoices only. Quotations, quotes, and "
    "estimates are pre-payment documents — issue or receive an invoice before paying."
)

RECEIPT_REJECTION_MESSAGE = (
    "This document is a receipt — proof of a payment already made. Paying it "
    "again would duplicate the payment. Verify against the unpaid invoice instead."
)

PURCHASE_ORDER_REJECTION_MESSAGE = (
    "This document is a purchase order — a commitment to buy, not a bill. Wait "
    "for the vendor's invoice, then verify the payment against it."
)

CREDIT_NOTE_REJECTION_MESSAGE = (
    "This document is a credit note or carries a negative total. Credit notes "
    "are out of scope for payment verification — process refunds through your "
    "accounts-payable workflow, not the payment gate."
)

STATEMENT_REJECTION_MESSAGE = (
    "This document is a statement of account — a summary of multiple invoices. "
    "Verify and pay the individual invoices it lists."
)

MULTI_CURRENCY_REJECTION_MESSAGE = (
    "This document shows amounts in more than one currency; AgentGate will not "
    "pick one by guessing. Verify against a single-currency invoice or route "
    "to a human."
)

_MONTHS = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "sept": "09", "oct": "10",
    "nov": "11", "dec": "12",
}

# ISO codes recognized as currency signals when adjacent to an amount.
KNOWN_CURRENCY_CODES = {
    "USD", "EUR", "GBP", "INR", "JPY", "CNY", "AUD", "CAD", "CHF", "SGD",
    "HKD", "NZD", "SEK", "NOK", "DKK", "AED", "SAR", "ZAR", "BRL", "MXN",
    "PLN",
}

_SYMBOL_TO_CODE = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY"}

# The first amount-shaped run on a line (an optional ISO code / symbol, then
# digits with any supported separators). Callers strip percent/parenthesized
# annotations first so a rate or "(USD)" is never misread as the amount.
_LINE_AMOUNT_RE = re.compile(
    r"\(?\s*-?\s*(?:[A-Z]{3}\s*)?[$€£₹¥]?\s*\d[\d.,\u00A0]*\s*\)?"
)


@dataclass
class ParsedMoney:
    value: str
    currency: str


@dataclass
class ParsedLineItem:
    description: str
    quantity: str
    unit_price: ParsedMoney
    amount: ParsedMoney
    kind: Literal["charge", "shipping", "discount", "tax"] = "charge"


@dataclass
class ParsedTaxLine:
    amount: ParsedMoney
    rate: Optional[str] = None


@dataclass
class ParsedInvoice:
    invoice_number: str
    vendor: str
    date: str
    currency: str
    line_items: list[ParsedLineItem]
    total: ParsedMoney
    raw_text: str
    tax_lines: list[ParsedTaxLine] = field(default_factory=list)
    subtotal: Optional[ParsedMoney] = None


def _canonical_number(raw: str) -> str:
    """Collapse separators losslessly (same rules as core grounding, D59)."""
    raw = raw.replace("\u00A0", "").replace(" ", "")
    if "." in raw and "," in raw:
        if raw.rfind(",") > raw.rfind("."):
            return raw.replace(".", "").replace(",", ".")
        return raw.replace(",", "")
    if "," in raw:
        head, _, tail = raw.rpartition(",")
        if len(tail) <= 2 and "," not in head:
            return f"{head}.{tail}"
        return raw.replace(",", "")
    if raw.count(".") > 1:
        return raw.replace(".", "")
    return raw


def parse_amount(raw: str) -> tuple[str, bool]:
    """Parse one amount cell to (two-decimal string, is_negative).

    Accepts US / European / Indian / NBSP-grouped separators (unambiguous forms
    only), currency symbols and ISO codes, and accounting negatives
    (parentheses or a minus sign). Raises ``ValueError`` on anything ambiguous.
    """
    s = raw.strip()
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative = True
        s = s[1:-1].strip()
    s = re.sub(
        r"\b[A-Z]{3}\b",
        lambda m: "" if m.group(0) in KNOWN_CURRENCY_CODES else m.group(0),
        s,
    )
    for symbol in _SYMBOL_TO_CODE:
        s = s.replace(symbol, "")
    s = s.strip()
    if s.startswith("-"):
        negative = True
        s = s[1:].strip()
    if s.endswith("-"):
        negative = True
        s = s[:-1].strip()
    core = _canonical_number(s)
    if not re.fullmatch(r"\d+(\.\d{1,2})?", core):
        raise ValueError(f"Could not parse money value: {raw}")
    whole, _, frac = core.partition(".")
    return f"{whole}.{(frac + '00')[:2]}", negative


def normalize_money(raw: str) -> str:
    """Backwards-compatible non-negative money normalization."""
    value, negative = parse_amount(raw)
    if negative:
        raise ValueError(f"Could not parse money value: {raw}")
    return value


def _title_case_vendor(line: str) -> str:
    return re.sub(r"\b\w", lambda m: m.group(0).upper(), line.strip().lower())


def _iso_date_from_words(raw: str) -> Optional[str]:
    """``July 14, 2026`` / ``Jul 14 2026`` / ``14 July 2026`` → ISO, else None."""
    mdy = re.fullmatch(
        r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", raw.strip()
    )
    if mdy:
        month = _MONTHS.get(mdy.group(1).lower())
        if month:
            return f"{mdy.group(3)}-{month}-{mdy.group(2).zfill(2)}"
    dmy = re.fullmatch(
        r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})", raw.strip()
    )
    if dmy:
        month = _MONTHS.get(dmy.group(2).lower())
        if month:
            return f"{dmy.group(3)}-{month}-{dmy.group(1).zfill(2)}"
    return None


_DATE_LABEL_RES = [
    re.compile(r"issue\s+date\s*[:#]?\s*(.+)", re.IGNORECASE),
    re.compile(r"invoice\s+date\s*[:#]?\s*(.+)", re.IGNORECASE),
    re.compile(r"date\s+of\s+issue\s*[:#]?\s*(.+)", re.IGNORECASE),
    re.compile(r"bill(?:ing)?\s+date\s*[:#]?\s*(.+)", re.IGNORECASE),
    re.compile(r"issued(?:\s+on)?\s*[:#]\s*(.+)", re.IGNORECASE),
    re.compile(r"^[ \t]*date\b(?![ \t]*due)\s*[:#]?\s*(.+)", re.IGNORECASE | re.MULTILINE),
]


def _extract_date(text: str) -> str:
    """Find the document's issue date. Unambiguous forms become ISO; ambiguous
    numeric forms (12/07/2026) pass through verbatim — no check consumes the
    date, and guessing day-vs-month order would be a lie."""
    for label_re in _DATE_LABEL_RES:
        match = label_re.search(text)
        if not match:
            continue
        tail = match.group(1).strip()
        iso = re.search(r"\d{4}-\d{2}-\d{2}", tail)
        if iso:
            return iso.group(0)
        worded = re.search(
            r"(?:[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
            r"|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?,?\s+\d{4})",
            tail,
        )
        if worded:
            converted = _iso_date_from_words(worded.group(0))
            if converted:
                return converted
        numeric = re.search(r"\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}", tail)
        if numeric:
            return numeric.group(0)
    bare_iso = re.search(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if bare_iso:
        return bare_iso.group(0)
    raise ValueError(
        "Could not find an issue date (looked for Issue Date / Invoice Date / "
        "Date of issue / Billing Date / Date)."
    )


_RECEIPT_RES = [
    re.compile(r"^[ \t]*(?:payment[ \t]+)?receipt\b(?![ \t]+of\b)", re.IGNORECASE | re.MULTILINE),
    re.compile(r"\breceipt\s*(?:#|no\.?|number)\s*[:#]?", re.IGNORECASE),
    re.compile(r"\bpayment\s+received\b", re.IGNORECASE),
    re.compile(r"\bpaid\s+in\s+full\b", re.IGNORECASE),
]

_PURCHASE_ORDER_RES = [
    re.compile(r"\bpurchase\s+order\b", re.IGNORECASE),
    re.compile(r"^[ \t]*P\.?O\.?\s*(?:#|no\.?|number)\s*[:#]", re.IGNORECASE | re.MULTILINE),
]

_CREDIT_NOTE_RE = re.compile(r"\bcredit\s+(?:note|memo)\b", re.IGNORECASE)

_STATEMENT_RES = [
    re.compile(r"\bstatement\s+of\s+account\b", re.IGNORECASE),
    re.compile(r"^[ \t]*account\s+statement\b", re.IGNORECASE | re.MULTILINE),
]

_QUOTATION_RES = [
    re.compile(r"\bQUOTATION\b", re.IGNORECASE),
    re.compile(r"\bQuote\s*#:", re.IGNORECASE),
    re.compile(r"\bQuote\s+Date:", re.IGNORECASE),
    re.compile(r"\bQuote\s+number\b", re.IGNORECASE),
    re.compile(r"\bEstimate\s*#:", re.IGNORECASE),
    re.compile(r"\bEstimate\s+Date:", re.IGNORECASE),
    re.compile(r"^[ \t]*estimate\b", re.IGNORECASE | re.MULTILINE),
]


def classify_document(text: str) -> None:
    """Reject non-payable money documents with a type-specific message.

    Credit notes / purchase orders / receipts / statements reject even when
    invoice markers are present (a PAID invoice must not be paid again); the
    quotation check applies only when NO invoice marker exists — a legitimate
    invoice routinely references its originating quotation (PRD §5b)."""
    if _CREDIT_NOTE_RE.search(text):
        raise ValueError(CREDIT_NOTE_REJECTION_MESSAGE)
    if any(p.search(text) for p in _PURCHASE_ORDER_RES):
        raise ValueError(PURCHASE_ORDER_REJECTION_MESSAGE)
    if any(p.search(text) for p in _RECEIPT_RES):
        raise ValueError(RECEIPT_REJECTION_MESSAGE)
    if any(p.search(text) for p in _STATEMENT_RES):
        raise ValueError(STATEMENT_REJECTION_MESSAGE)
    if not INVOICE_MARKER_RE.search(text):
        if any(p.search(text) for p in _QUOTATION_RES):
            raise ValueError(QUOTATION_REJECTION_MESSAGE)


def detect_currency(text: str) -> str:
    """Resolve the document currency from ISO codes adjacent to amounts, an
    explicit Currency label, or currency symbols. Two distinct signals reject —
    the §7 dual-amount ambiguity rule, applied upstream."""
    signals: set[str] = set()
    label = re.search(r"^[ \t]*currency\s*[:#]?\s*([A-Za-z]{3})\b", text, re.IGNORECASE | re.MULTILINE)
    if label and label.group(1).upper() in KNOWN_CURRENCY_CODES:
        signals.add(label.group(1).upper())
    for match in re.finditer(r"\b([A-Z]{3})\s*[$€£₹¥]?\s?\d", text):
        if match.group(1) in KNOWN_CURRENCY_CODES:
            signals.add(match.group(1))
    for match in re.finditer(r"\d[\d.,\u00A0]*[ \t]*([A-Z]{3})\b", text):
        if match.group(1) in KNOWN_CURRENCY_CODES:
            signals.add(match.group(1))
    for symbol, code in _SYMBOL_TO_CODE.items():
        if symbol in text:
            signals.add(code)
    if len(signals) > 1:
        raise ValueError(MULTI_CURRENCY_REJECTION_MESSAGE)
    return signals.pop() if signals else "USD"


def _strip_annotations(line_tail: str) -> str:
    """Remove rate/code annotations so they are never misread as the amount:
    percent tokens (``20%``), parenthesized groups containing a percent or only
    a currency code. Parenthesized negatives (``(1,240.00)``) survive."""
    tail = re.sub(r"\([^)]*%[^)]*\)", " ", line_tail)
    tail = re.sub(r"\(\s*[A-Z]{3}\s*\)", " ", tail)
    tail = re.sub(r"\d+(?:[.,]\d+)?\s*%", " ", tail)
    return tail


def _first_amount(line_tail: str) -> Optional[str]:
    match = _LINE_AMOUNT_RE.search(_strip_annotations(line_tail))
    return match.group(0) if match else None


_INVOICE_NUMBER_RES = [
    re.compile(
        r"(?:tax\s+invoice|invoice|bill)\s*(?:#|no\.?|num(?:ber)?|id)\s*[:#]?\s*([A-Za-z0-9][\w./-]*)",
        re.IGNORECASE,
    ),
    re.compile(r"^[ \t]*(?:tax\s+)?invoice[ \t:#]+([A-Za-z0-9][\w./-]*)[ \t]*$", re.IGNORECASE | re.MULTILINE),
]

_TABLE_HEADER_RE = re.compile(r"^(?:description|item|details|particulars)\b", re.IGNORECASE)

_SUMMARY_LABEL_RE = re.compile(
    r"^[ \t]*(?:sub\s*-?\s*total|total|amount\s+due|balance\s+due|grand\s+total|"
    r"sales\s+tax|value\s+added\s+tax|vat\b|gst\b|hst\b|pst\b|igst\b|cgst\b|sgst\b|tax\b)",
    re.IGNORECASE,
)

_TAX_LABEL_RE = re.compile(
    r"^[ \t]*(sales\s+tax|value\s+added\s+tax|vat|gst|hst|pst|igst|cgst|sgst|tax)\b(.*)$",
    re.IGNORECASE,
)

_MONEY_CELL = r"\(?-?[$€£₹¥]?[\d][\d.,\u00A0]*\)?"

_CLASSIC_ROW_RE = re.compile(
    rf"^[ \t]*(.+?)[ \t]{{2,}}(-|\d+(?:\.\d+)?)[ \t]+(-|{_MONEY_CELL})[ \t]+({_MONEY_CELL})[ \t]*$"
)

_STRIPE_ROW_RE = re.compile(
    r"^[ \t]*(.+?)[ \t]+(\d+(?:\.\d+)?)[ \t]+\$?([\d.,\u00A0]+)[ \t]+[\d.]+%[ \t]+\$?([\d.,\u00A0]+)\s*$"
)

# Total labels in priority order — the most specific payable-amount labels win.
_TOTAL_LABEL_RES = [
    re.compile(r"^[ \t]*(?:total[ \t]+)?amount[ \t]+due\b(.*)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*total[ \t]+due\b(.*)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*balance[ \t]+due\b(.*)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*grand[ \t]+total\b(.*)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*(?:total|amount)[ \t]+payable\b(.*)$", re.IGNORECASE | re.MULTILINE),
    re.compile(r"^[ \t]*total[ \t]+amount\b(.*)$", re.IGNORECASE | re.MULTILINE),
    re.compile(
        r"^[ \t]*total\b(?![ \t]*(?:excluding|excl))(.*)$", re.IGNORECASE | re.MULTILINE
    ),
]

_SUBTOTAL_RE = re.compile(
    r"^[ \t]*(?:sub\s*-?\s*total|total[ \t]+excluding[ \t]+tax|net[ \t]+(?:total|amount))\b(.*)$",
    re.IGNORECASE | re.MULTILINE,
)

_TOTAL_NOT_FOUND_MESSAGE = (
    "Could not find the payable total (looked for Amount Due / Total Due / "
    "Balance Due / Grand Total / Total Payable / Total Amount / Total)."
)

_NUMBER_NOT_FOUND_MESSAGE = (
    "Could not find an invoice number (looked for Invoice #/No./Number/ID, "
    "Bill No., Tax Invoice, or an INVOICE <reference> title line)."
)


def parse_invoice_text(raw_text: str) -> ParsedInvoice:
    text = (
        raw_text.replace("\r\n", "\n")
        .translate({c: None for c in range(32) if c not in (9, 10) and c != 127})
        .strip()
    )
    if not text:
        raise ValueError("Invoice text is empty.")
    classify_document(text)
    if re.search(r"Invoice number", text, re.IGNORECASE) and re.search(
        r"Date of issue|Amount due", text, re.IGNORECASE
    ):
        return _parse_stripe_style(text)
    return _parse_classic(text)


def _parse_money_cell(cell: str, currency: str) -> tuple[str, bool]:
    return parse_amount(cell)


def _line_item_kind(description: str, negative: bool) -> Literal["charge", "shipping", "discount", "tax"]:
    if negative or re.search(r"\b(?:discount|rebate|promo(?:tion)?|credit)\b", description, re.IGNORECASE):
        return "discount"
    if re.search(r"\b(?:shipping|freight|delivery|postage)\b", description, re.IGNORECASE):
        return "shipping"
    if re.search(r"\b(?:tax|vat|gst)\b", description, re.IGNORECASE):
        return "tax"
    return "charge"


def _extract_tax_lines(
    lines: list[str], table_region: set[int], currency: str
) -> list[ParsedTaxLine]:
    tax_lines: list[ParsedTaxLine] = []
    for idx, line in enumerate(lines):
        if idx in table_region:
            continue
        match = _TAX_LABEL_RE.match(line)
        if not match:
            continue
        rest = match.group(2)
        # "Tax ID 55-1234567" / "VAT Registration ..." are identifiers, not amounts.
        if re.match(r"^[ \t]*(?:id|reg)", rest, re.IGNORECASE):
            continue
        # "VAT included" style annotations are informational, never additive.
        if re.search(r"includ", line, re.IGNORECASE):
            continue
        rate_match = re.search(r"(\d+(?:[.,]\d+)?)\s*%", rest)
        cell = _first_amount(rest)
        if cell is None:
            continue
        try:
            value, negative = parse_amount(cell)
        except ValueError:
            continue
        if negative:
            continue
        rate: Optional[str] = None
        if rate_match:
            try:
                # normalize()+"f" so "0.0%" emits "0", matching the TS mirror
                rate = format(
                    (Decimal(rate_match.group(1).replace(",", ".")) / Decimal(100)).normalize(),
                    "f",
                )
            except InvalidOperation:
                rate = None
        tax_lines.append(ParsedTaxLine(amount=ParsedMoney(value=value, currency=currency), rate=rate))
    return tax_lines


def _extract_total(text: str) -> str:
    for total_re in _TOTAL_LABEL_RES:
        for match in total_re.finditer(text):
            cell = _first_amount(match.group(1))
            if cell is None:
                continue
            try:
                value, negative = parse_amount(cell)
            except ValueError:
                continue
            if negative:
                raise ValueError(CREDIT_NOTE_REJECTION_MESSAGE)
            return value
    raise ValueError(_TOTAL_NOT_FOUND_MESSAGE)


def _extract_subtotal(text: str, currency: str) -> Optional[ParsedMoney]:
    for match in _SUBTOTAL_RE.finditer(text):
        cell = _first_amount(match.group(1))
        if cell is None:
            continue
        try:
            value, negative = parse_amount(cell)
        except ValueError:
            continue
        if not negative:
            return ParsedMoney(value=value, currency=currency)
    return None


def _parse_classic(text: str) -> ParsedInvoice:
    lines = text.split("\n")
    vendor = _title_case_vendor(next((l for l in lines if l.strip()), "Unknown Vendor"))

    invoice_number: Optional[str] = None
    for number_re in _INVOICE_NUMBER_RES:
        match = number_re.search(text)
        if match:
            invoice_number = match.group(1).rstrip(".,;:")
            break
    if not invoice_number:
        raise ValueError(_NUMBER_NOT_FOUND_MESSAGE)

    date = _extract_date(text)
    currency = detect_currency(text)

    line_items: list[ParsedLineItem] = []
    table_region: set[int] = set()
    in_table = False
    for idx, line in enumerate(lines):
        if not in_table:
            if _TABLE_HEADER_RE.match(line.strip()):
                in_table = True
                table_region.add(idx)
            continue
        if re.match(r"^-{4,}", line) or not line.strip():
            table_region.add(idx)
            continue
        row = _CLASSIC_ROW_RE.match(line)
        parsed_row = None
        if row:
            description, qty_raw, unit_raw, amount_raw = row.groups()
            try:
                amount_value, amount_negative = parse_amount(amount_raw)
                unit_value = (
                    amount_value if unit_raw == "-" else parse_amount(unit_raw)[0]
                )
                parsed_row = (description, qty_raw, unit_value, amount_value, amount_negative)
            except ValueError:
                parsed_row = None
        if parsed_row is not None:
            description, qty_raw, unit_value, amount_value, amount_negative = parsed_row
            line_items.append(
                ParsedLineItem(
                    description=description.strip(),
                    quantity="1" if qty_raw == "-" else qty_raw,
                    unit_price=ParsedMoney(value=unit_value, currency=currency),
                    amount=ParsedMoney(value=amount_value, currency=currency),
                    kind=_line_item_kind(description, amount_negative),
                )
            )
            table_region.add(idx)
            continue
        if _SUMMARY_LABEL_RE.match(line):
            break
        table_region.add(idx)

    total_value = _extract_total(text)
    tax_lines = _extract_tax_lines(lines, table_region, currency) if line_items else []

    return ParsedInvoice(
        invoice_number=invoice_number,
        vendor=vendor,
        date=date,
        currency=currency,
        line_items=line_items,
        tax_lines=tax_lines,
        subtotal=_extract_subtotal(text, currency),
        total=ParsedMoney(value=total_value, currency=currency),
        raw_text=text,
    )


def _parse_stripe_style(text: str) -> ParsedInvoice:
    number_match = re.search(r"Invoice number[ \t]+(.+)", text, re.IGNORECASE)
    if not number_match:
        raise ValueError("Could not find Invoice number.")
    invoice_number = re.sub(r"\s+", "-", number_match.group(1).strip())

    date = _extract_date(text)

    vendor_match = re.search(r"^(.+?)[ \t]{2,}Bill to\b", text, re.IGNORECASE | re.MULTILINE)
    vendor = vendor_match.group(1).strip() if vendor_match else ""
    if not vendor:
        flat_lines = [line.strip() for line in text.split("\n")]
        bill_to_idx = next(
            (i for i, line in enumerate(flat_lines) if re.match(r"^Bill to\b", line, re.I)),
            len(flat_lines),
        )
        for line in flat_lines[:bill_to_idx]:
            if not line:
                continue
            if re.match(
                r"^(page \d|invoice\b|date of issue|date due|expiration\b|vat\b)",
                line,
                re.I,
            ):
                continue
            if re.fullmatch(r"[A-Z0-9]{6,}", line):
                continue
            vendor = line
            break
    if not vendor:
        raise ValueError("Could not find the issuing vendor (line before 'Bill to').")

    currency = detect_currency(text)

    lines = text.split("\n")
    line_items: list[ParsedLineItem] = []
    table_region: set[int] = set()
    in_table = False
    for idx, line in enumerate(lines):
        if not in_table:
            if re.match(r"^Description\b", line.strip(), re.IGNORECASE):
                in_table = True
                table_region.add(idx)
            continue
        if re.match(r"^\s*Subtotal\b", line, re.IGNORECASE):
            break
        row = _STRIPE_ROW_RE.match(line)
        table_region.add(idx)
        if not row:
            continue
        description, quantity, unit_raw, amount_raw = row.groups()
        try:
            unit_value, _ = parse_amount(unit_raw)
            amount_value, amount_negative = parse_amount(amount_raw)
        except ValueError:
            continue
        line_items.append(
            ParsedLineItem(
                description=description.strip(),
                quantity=quantity,
                unit_price=ParsedMoney(value=unit_value, currency=currency),
                amount=ParsedMoney(value=amount_value, currency=currency),
                kind=_line_item_kind(description, amount_negative),
            )
        )

    total_value = _extract_total(text)
    tax_lines = _extract_tax_lines(lines, table_region, currency) if line_items else []

    return ParsedInvoice(
        invoice_number=invoice_number,
        vendor=vendor,
        date=date,
        currency=currency,
        line_items=line_items,
        tax_lines=tax_lines,
        subtotal=_extract_subtotal(text, currency),
        total=ParsedMoney(value=total_value, currency=currency),
        raw_text=text,
    )


def parsed_invoice_to_wire(parsed: ParsedInvoice) -> dict:
    """Convert a parsed invoice to the ``source.invoice`` wire shape."""
    invoice: dict = {
        "invoice_number": parsed.invoice_number,
        "vendor": parsed.vendor,
        "date": parsed.date,
        "currency": parsed.currency,
        "line_items": [
            {
                "description": item.description,
                "quantity": item.quantity,
                "unit_price": {"value": item.unit_price.value, "currency": item.unit_price.currency},
                "amount": {"value": item.amount.value, "currency": item.amount.currency},
                "kind": item.kind,
            }
            for item in parsed.line_items
        ],
        "tax_lines": [
            (
                {"rate": tl.rate, "amount": {"value": tl.amount.value, "currency": tl.amount.currency}}
                if tl.rate is not None
                else {"amount": {"value": tl.amount.value, "currency": tl.amount.currency}}
            )
            for tl in parsed.tax_lines
        ],
        "total": {"value": parsed.total.value, "currency": parsed.total.currency},
    }
    if parsed.subtotal is not None:
        invoice["subtotal"] = {
            "value": parsed.subtotal.value,
            "currency": parsed.subtotal.currency,
        }
    return invoice
