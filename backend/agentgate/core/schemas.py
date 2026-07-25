"""Core Pydantic data models for AgentGate.

Hard rule (DECISIONS D1): money is ``Decimal`` parsed from a string or int —
NEVER float. A single float in the path reintroduces `0.1 + 0.2 != 0.3`
rounding lies and would break the trustworthy deterministic core.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Caller-text bounds (PRD SS6, D34): generous ceilings, applied uniformly —
# every wire-model string flows verbatim into the Decision echo
# (proposed_action), check detail/message strings, Langfuse traces, and the
# dashboard, so every one is bounded. Numeric strings are capped BEFORE the
# Decimal parse: a million-digit "amount" is an arithmetic/rendering DoS, and
# an uncapped value would ride validator messages into traces. Above a bound,
# validation fails and the request fail-closes to ESCALATE — over-rejection is
# the fail-closed direction.
MAX_IDENTIFIER_LENGTH = 100
MAX_VENDOR_LENGTH = 200
MAX_DATE_LENGTH = 50
MAX_CURRENCY_LENGTH = 10
MAX_DESCRIPTION_LENGTH = 500
MAX_RATIONALE_LENGTH = 2000
MAX_ADJUSTMENTS = 20
MAX_ADJUSTMENT_CHARS = 500
MAX_NUMERIC_CHARS = 50
# List and raw-text bounds (PRD SS6/SS9, D36): same exposure, same treatment as
# the string bounds above. Documents beyond these ceilings are exotic-format
# territory (PRD SS7) and escalate by failing validation.
MAX_LINE_ITEMS = 500
MAX_TAX_LINES = 50
MAX_RAW_TEXT_LENGTH = 50_000

QUOTATION_RAW_TEXT_MESSAGE = (
    "AgentGate verifies payments against invoices only. Quotations, quotes, and "
    "estimates are pre-payment documents — issue or receive an invoice before paying."
)

CALLER_RAW_TEXT_REQUIRED_MESSAGE = (
    "Caller-supplied evidence requires source.raw_text — the original invoice "
    "document text. AgentGate does not verify agent-forged JSON alone. Use "
    "source.fetch to resolve from your system of record."
)

_QUOTATION_RAW_TEXT_PATTERNS = (
    re.compile(r"\bQUOTATION\b", re.IGNORECASE),
    re.compile(r"\bQuote\s*#:", re.IGNORECASE),
    re.compile(r"\bQuote\s+Date:", re.IGNORECASE),
    re.compile(r"\bQuote\s+number\b", re.IGNORECASE),
    re.compile(r"\bEstimate\s*#:", re.IGNORECASE),
    re.compile(r"\bEstimate\s+Date:", re.IGNORECASE),
)

# An invoice marker anywhere in the text means the document is invoice-shaped:
# a real invoice routinely REFERENCES its originating quotation ("as per
# quotation Q-123"), so quote wording alone must not reject it (PRD SS5b
# invoice-marker precedence). Shared with the upstream parser — one definition,
# no drift.
INVOICE_MARKER_RE = re.compile(
    r"(?:\btax\s+invoice\b)|(?:\binvoice\s*(?:#|no\.?|num(?:ber)?|id)\b)|(?:^[ \t]*invoice\b)",
    re.IGNORECASE | re.MULTILINE,
)


def _reject_quotation_raw_text(raw_text: str) -> None:
    """Payment verification belongs at invoice time, not quote time. Quote
    wording only rejects when no invoice marker is present (see above)."""
    if INVOICE_MARKER_RE.search(raw_text):
        return
    for pattern in _QUOTATION_RAW_TEXT_PATTERNS:
        if pattern.search(raw_text):
            raise ValueError(QUOTATION_RAW_TEXT_MESSAGE)

# Every caller-facing wire model is extra="forbid" (PRD SS6/SS9, D36): an
# unknown or misspelled field is a ValidationError -> fail-closed ESCALATE,
# never a silent drop. Load-bearing, not pedantry: a misspelled `adjustments`
# key silently dropped would default to [] and flip a declared-adjustment
# ESCALATE into an agent-fixable BLOCK whose fixer then pays the full total.
# Response-side models (Decision, Check, BlockReason) are not forbid — we
# construct them.


def _normalized_identifier(v: object, field_name: str) -> str:
    """Strip surrounding whitespace (a lossless transport artifact, never part of
    an identifier) and require a non-empty result. Case, internal spacing, and
    punctuation are preserved — an identifier has no lossless entity-level
    canonical form (D8). This normalized value is what the duplicate store keys
    on, so normalization lives here at the schema boundary, not in the frame
    check: otherwise ``" INV-001"`` and ``"INV-001"`` are distinct SQLite primary
    keys and one leading space silently defeats the duplicate check (D31)."""
    if not isinstance(v, str):
        raise ValueError(f"{field_name} must be a string.")
    v = v.strip()
    if not v:
        raise ValueError(
            f"{field_name} must be a non-empty identifier "
            "(min length 1 after stripping surrounding whitespace)."
        )
    if len(v) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(
            f"{field_name} is too long ({len(v)} characters, "
            f"max {MAX_IDENTIFIER_LENGTH})."
        )
    return v


def _normalized_currency(v: str) -> str:
    """Shared by ``Money.currency`` and ``Invoice.currency`` — the two are
    compared by ``currency_match``, so they get identical treatment (D12/D34)."""
    v = v.strip()
    if not v:
        raise ValueError("currency is required and must be non-empty.")
    if len(v) > MAX_CURRENCY_LENGTH:
        raise ValueError(
            f"currency is too long ({len(v)} characters, max {MAX_CURRENCY_LENGTH})."
        )
    return v


# A generous magnitude ceiling for a caller-supplied amount (a quintillion). Like
# the length bounds above it is a "generous ceiling, not format validation" (D34):
# it keeps every amount — and every sum of up to MAX_LINE_ITEMS+MAX_TAX_LINES of
# them — inside the 28-significant-digit Decimal context, so structural_arithmetic
# stays EXACT (never silently rounds a real discrepancy away) and can never trip a
# decimal overflow. It lives on the CALLER models (Invoice/ProposedAction), never
# on Money itself, because the verifier legitimately builds a Money from a computed
# sum that may be negative or larger than any single input.
_AMOUNT_MAX = Decimal("1e18")


def _coerce_decimal(v: object, field_name: str) -> object:
    """Shared numeric coercion for every Decimal wire field (``Money.value``,
    ``quantity``, ``TaxLine.rate``): reject bool/float (D1), cap the raw length
    BEFORE the parse, parse strings / ints / already-decoded Decimals to Decimal,
    then require the result to be finite and bounded in significant and fractional
    digits.

    The digit caps are the D34 bound applied on EVERY entry path, not just the
    string one. A JSON number decoded upstream with ``parse_float=Decimal`` reaches
    a validator as a ``Decimal`` and a JSON integer reaches it as an ``int`` — both
    previously skipped the 50-char string cap and could ride a multi-thousand-digit
    value into check details, the Decision echo, and Langfuse traces."""
    if isinstance(v, bool):  # bool is an int subclass — not a number here
        raise ValueError(f"{field_name} must be a number, not a bool.")
    if isinstance(v, float):
        raise ValueError(
            f"{field_name} must be a string or int, never float "
            "(float introduces rounding error; see DECISIONS D1)."
        )
    if isinstance(v, str):
        s = v.strip()
        if len(s) > MAX_NUMERIC_CHARS:
            # Report the length, never the value — this can end up in a
            # fail-closed reason (D34).
            raise ValueError(
                f"{field_name} string is too long ({len(s)} characters, "
                f"max {MAX_NUMERIC_CHARS})."
            )
        try:
            d = Decimal(s)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{field_name} is not a valid decimal: {s!r}") from exc
    elif isinstance(v, int):
        s = str(v)
        if len(s) > MAX_NUMERIC_CHARS:
            raise ValueError(
                f"{field_name} integer is too long ({len(s)} digits, "
                f"max {MAX_NUMERIC_CHARS})."
            )
        d = Decimal(v)
    elif isinstance(v, Decimal):
        d = v
    else:
        return v  # unknown type: let pydantic raise its own typed error
    if not d.is_finite():
        # Belt to pydantic-core's own non-finite rejection: a NaN reaching a
        # comparison downstream raises InvalidOperation, which would escape an
        # ``except ValidationError`` fail-closed catch (D34's TaxLine.rate lesson).
        raise ValueError(f"{field_name} must be a finite number (no NaN/Infinity).")
    _, digits, exp = d.as_tuple()
    if len(digits) > MAX_NUMERIC_CHARS:
        raise ValueError(
            f"{field_name} has too many significant digits "
            f"({len(digits)}, max {MAX_NUMERIC_CHARS})."
        )
    if -exp > MAX_NUMERIC_CHARS:
        raise ValueError(
            f"{field_name} has too many fractional digits (max {MAX_NUMERIC_CHARS})."
        )
    return d


def _adjustment_json_default(o: object) -> str:
    """``json.dumps`` fallback for the adjustments size check: rescue only
    ``Decimal`` (a JSON number decoded upstream with ``parse_float=Decimal``) so
    numeric adjustments are bounded identically on the HTTP and MCP boundaries.
    Anything else is genuinely un-echoable and must still raise (it would crash
    the later ``model_dump`` of the Decision echo)."""
    if isinstance(o, Decimal):
        return str(o)
    raise TypeError(f"adjustment value of type {type(o).__name__} is not serializable")


def _require_bounded_amount(value: Decimal, field_name: str) -> None:
    """A caller-supplied amount must be non-negative and within ``_AMOUNT_MAX``.

    Negative amounts are credit notes / refunds — explicitly out of scope in v1
    (PRD SS7): reject them so they fail closed to ESCALATE rather than sailing to
    ALLOW as an ordinary payment (a self-consistent negative invoice otherwise
    scores 1.0). The magnitude ceiling keeps sums exact and overflow-free. ``value``
    is already finite (``_coerce_decimal``), so the comparisons cannot raise."""
    if value < 0:
        raise ValueError(
            f"{field_name} must not be negative ({value}); credit notes and refunds "
            "are out of scope in v1 — route to a human."
        )
    if value >= _AMOUNT_MAX:
        raise ValueError(
            f"{field_name} exceeds the maximum supported magnitude ({_AMOUNT_MAX})."
        )


class Money(BaseModel):
    """A monetary amount. ``value`` is a ``Decimal`` parsed from a string/int;
    ``currency`` is first-class and required (DECISIONS D1/D12)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: Decimal
    currency: str

    @field_validator("value", mode="before")
    @classmethod
    def _no_float(cls, v: object) -> object:
        """Reject float, parse to Decimal, and bound precision on every entry path.

        Floats are refused because they carry rounding error (D1); JSON should be
        decoded with ``parse_float=Decimal`` upstream so a float never reaches
        here. The magnitude/sign bound lives on the caller models, not here —
        ``Money`` is also constructed internally from computed sums that may be
        negative or large (see ``_coerce_decimal`` / ``_require_bounded_amount``)."""
        return _coerce_decimal(v, "Money.value")

    @field_validator("currency")
    @classmethod
    def _currency_present(cls, v: str) -> str:
        return _normalized_currency(v)


class LineItemKind(str, Enum):
    charge = "charge"
    discount = "discount"
    shipping = "shipping"
    tax = "tax"


class LineItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(max_length=MAX_DESCRIPTION_LENGTH)
    quantity: Decimal
    unit_price: Money
    amount: Money
    kind: LineItemKind = LineItemKind.charge

    @field_validator("quantity", mode="before")
    @classmethod
    def _quantity_no_float(cls, v: object) -> object:
        """Quantity is ``Decimal`` (fractional billing — 2.5 hours, 1.5 kg — is
        legitimate) parsed from a string or int, never float (D1/D29). It is
        consumed by no check, so widening it only stops rejecting valid invoices.
        Bounds (incl. the JSON int/Decimal paths) come from ``_coerce_decimal``."""
        return _coerce_decimal(v, "quantity")


class TaxLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rate: Optional[Decimal] = None
    amount: Money

    @field_validator("rate", mode="before")
    @classmethod
    def _rate_no_float(cls, v: object) -> object:
        """Tax rate is ``Decimal``, never float (D1), and OPTIONAL — many real
        documents state a tax amount with no rate, and fabricating one would be
        a lie; no check consumes ``rate``, only tax amounts feed arithmetic and
        coverage (PRD §6). ``_coerce_decimal`` also caps length/precision and
        rejects NaN/Infinity on every entry path — without that a junk rate
        escaped ``model_validate`` as a raw ``InvalidOperation``, bypassing an
        ``except ValidationError`` fail-closed catch (D34)."""
        if v is None:
            return None
        return _coerce_decimal(v, "tax rate")


class Invoice(BaseModel):
    """The caller-supplied source document. ``subtotal`` is accepted although no
    check consumes it — it is a field of the invoice document itself, not a
    separate evidence artifact promising a check (contrast ``source.po``, D36)."""

    model_config = ConfigDict(extra="forbid")

    invoice_number: str
    vendor: str = Field(max_length=MAX_VENDOR_LENGTH)
    date: str = Field(max_length=MAX_DATE_LENGTH)
    currency: str
    line_items: list[LineItem] = Field(default_factory=list, max_length=MAX_LINE_ITEMS)
    subtotal: Optional[Money] = None
    tax_lines: list[TaxLine] = Field(default_factory=list, max_length=MAX_TAX_LINES)
    total: Money

    @field_validator("invoice_number")
    @classmethod
    def _normalize_invoice_number(cls, v: str) -> str:
        return _normalized_identifier(v, "invoice_number")

    @field_validator("currency")
    @classmethod
    def _normalize_currency(cls, v: str) -> str:
        return _normalized_currency(v)

    @model_validator(mode="after")
    def _amounts_bounded(self) -> "Invoice":
        """Every caller-supplied amount is non-negative and within ``_AMOUNT_MAX``
        (negative = out-of-scope credit note; huge = arithmetic/precision hazard).
        Enforced on the whole document at once so the arithmetic inputs
        (``total``, line amounts, tax amounts) are all bounded before the verifier
        sums them."""
        _require_bounded_amount(self.total.value, "invoice.total")
        if self.subtotal is not None:
            _require_bounded_amount(self.subtotal.value, "invoice.subtotal")
        for i, li in enumerate(self.line_items):
            _require_bounded_amount(li.amount.value, f"line_items[{i}].amount")
            _require_bounded_amount(li.unit_price.value, f"line_items[{i}].unit_price")
        for i, tl in enumerate(self.tax_lines):
            _require_bounded_amount(tl.amount.value, f"tax_lines[{i}].amount")
        return self


class ActionType(str, Enum):
    approve_payment = "approve_payment"
    flag = "flag"
    reject = "reject"


class ProposedAction(BaseModel):
    """What the agent wants to do. ``adjustments`` are declared only and
    NOT verified in v1 — any non-empty diff escalates (D13/D17)."""

    model_config = ConfigDict(extra="forbid")

    action_type: ActionType
    invoice_number: str
    amount: Money
    vendor: str = Field(max_length=MAX_VENDOR_LENGTH)
    adjustments: list = []
    agent_rationale: str = Field(default="", max_length=MAX_RATIONALE_LENGTH)

    @field_validator("invoice_number")
    @classmethod
    def _normalize_invoice_number(cls, v: str) -> str:
        return _normalized_identifier(v, "invoice_number")

    @field_validator("adjustments")
    @classmethod
    def _bounded_adjustments(cls, v: list) -> list:
        """Adjustments are declared, unverified labels (D13) echoed verbatim in
        the Decision, so bound their count and per-item serialized size while
        staying shape-agnostic — typing them now would speculate on the deferred
        adjustment-verification milestone (D34).

        The ``Decimal`` default keeps the size check identical across both
        boundaries: over HTTP a numeric adjustment is decoded with
        ``parse_float=Decimal`` and would otherwise make ``json.dumps`` raise
        ``TypeError`` on the ``Decimal`` — so the same ``[{"delta": 1.5}]`` that the
        MCP boundary accepts (and routes to a declared-adjustment ESCALATE, D13)
        would spuriously fail-close over HTTP. Only ``Decimal`` is rescued;
        genuinely un-echoable values still raise (they would otherwise crash the
        later ``model_dump`` of the Decision echo)."""
        if len(v) > MAX_ADJUSTMENTS:
            raise ValueError(f"adjustments has {len(v)} items (max {MAX_ADJUSTMENTS}).")
        for i, item in enumerate(v):
            try:
                encoded = json.dumps(item, default=_adjustment_json_default)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"adjustments[{i}] is not JSON-serializable.") from exc
            if len(encoded) > MAX_ADJUSTMENT_CHARS:
                raise ValueError(
                    f"adjustments[{i}] serializes to {len(encoded)} characters "
                    f"(max {MAX_ADJUSTMENT_CHARS})."
                )
        return v

    @model_validator(mode="after")
    def _amount_bounded(self) -> "ProposedAction":
        """The proposed payment amount is non-negative and within ``_AMOUNT_MAX``
        (a negative amount is an out-of-scope credit note, not an ordinary
        payment; see ``_require_bounded_amount``)."""
        _require_bounded_amount(self.amount.value, "proposed_action.amount")
        return self


class CheckKind(str, Enum):
    critical = "critical"
    soft = "soft"


class Check(BaseModel):
    """One verification check's result (a row in the Decision's checks table)."""

    name: str
    type: CheckKind
    passed: bool
    detail: str = ""


class BlockType(str, Enum):
    agent_fixable = "agent_fixable"  # the action misread a valid source; retry (cap 2)
    source_invalid = "source_invalid"  # the source is internally inconsistent; do not retry


class BlockReason(BaseModel):
    """Machine-readable reason attached to a BLOCK or an ESCALATE (PRD §6).

    ``expected``/``received`` are ``Money`` for money-valued checks and plain
    strings for non-money ones (vendor). ``block_type`` is set for the two block
    kinds (D7) and left ``None`` for other escalate reasons (currency, vendor,
    duplicate, declared-adjustment)."""

    check: str
    expected: Optional[Money | str] = None
    received: Optional[Money | str] = None
    field_to_change: Optional[str] = None
    block_type: Optional[BlockType] = None
    message: str


class DecisionType(str, Enum):
    allow = "allow"
    block = "block"
    escalate = "escalate"


class Decision(BaseModel):
    """What AgentGate returns (PRD §6). ``trace_id``/``latency_ms``/``timestamp``
    are populated at the API boundary; the core decision logic is pure."""

    decision: DecisionType
    # Optional: None means the decision scored nothing to score against — a
    # frame-stage escalate (content checks never ran) or a malformed-input
    # fail-closed escalate — which is distinct from 0 (measured, all failed). D32.
    score: Optional[Decimal]
    checks: list[Check] = []
    reasons: list[BlockReason] = []
    evidence_used: list[str] = []
    proposed_action: Optional[ProposedAction] = None
    trace_id: Optional[str] = None
    latency_ms: Optional[int] = None
    timestamp: Optional[str] = None


# --- HTTP request envelope (PRD SS9, Slice 7a) ----------------------------------


class Source(BaseModel):
    """The evidence for a verification (PRD SS0/SS9) — a two-mode union (D45).

    Caller mode: ``invoice`` plus **required** ``raw_text`` (the original invoice
    document). Structured JSON alone is rejected — enterprise deployments must
    attach source text for grounding or use fetch mode. Fetch mode: ``fetch``
    names an invoice number to resolve from the operator-configured system of
    record — and nothing else may be supplied, or the ``system_of_record:``
    provenance stamped on the Decision would label evidence the caller shaped.
    Exactly one mode; the validator enforces it.

    There is deliberately no ``po`` field: the ``po_match`` check does not exist
    yet, and accepting evidence nothing reads would make ALLOW overclaim —
    ``extra="forbid"`` rejects it until the check ships (D36)."""

    model_config = ConfigDict(extra="forbid")

    invoice: Optional[Invoice] = None
    raw_text: Optional[str] = Field(default=None, max_length=MAX_RAW_TEXT_LENGTH)
    fetch: Optional[str] = None

    @field_validator("fetch")
    @classmethod
    def _normalize_fetch(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        return _normalized_identifier(v, "source.fetch")

    @field_validator("raw_text")
    @classmethod
    def _invoice_only_raw_text(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            _reject_quotation_raw_text(v)
        return v

    @model_validator(mode="after")
    def _exactly_one_mode(self) -> "Source":
        if self.fetch is not None:
            if self.invoice is not None or self.raw_text is not None:
                raise ValueError(
                    "source.fetch cannot be combined with caller-supplied evidence "
                    "(invoice/raw_text): fetched evidence comes only from the "
                    "system of record."
                )
        elif self.invoice is None:
            raise ValueError(
                "source requires either a structured invoice (caller-supplied "
                "evidence) or fetch (an invoice number to resolve from the "
                "system of record)."
            )
        elif self.raw_text is None or not self.raw_text.strip():
            raise ValueError(CALLER_RAW_TEXT_REQUIRED_MESSAGE)
        return self


class VerifyRequest(BaseModel):
    """The POST /verify body (PRD SS9): the proposed action plus the evidence to
    verify it against. Policy is server-side config, never part of the request —
    a caller-supplied policy would be a fail-open vector."""

    model_config = ConfigDict(extra="forbid")

    proposed_action: ProposedAction
    source: Source
