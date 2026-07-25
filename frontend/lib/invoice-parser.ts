/**
 * Deterministic plain-text money-document parser (upstream layer).
 *
 * MIRRORS backend/agentgate/upstream/invoice_text.py — one parsing contract
 * (PRD §5b); change both or neither. Parse payable documents (invoices/bills)
 * across layouts, labels, currencies, and number formats; recognize
 * non-payable money documents (quotations, receipts, purchase orders, credit
 * notes, statements) and reject each with an instructive, type-specific
 * message; reject ambiguity (two currencies, negative totals) rather than
 * guess. Money stays strings end to end (D1) — no float ever exists.
 */

export interface Money {
  value: string;
  currency: string;
}

export interface LineItem {
  description: string;
  quantity: string;
  unit_price: Money;
  amount: Money;
  kind: "charge" | "shipping" | "discount" | "tax";
}

export interface TaxLineWire {
  rate?: string;
  amount: Money;
}

export interface ParsedInvoice {
  invoice_number: string;
  vendor: string;
  date: string;
  currency: string;
  line_items: LineItem[];
  tax_lines: TaxLineWire[];
  subtotal?: Money;
  total: Money;
  raw_text: string;
}

export const QUOTATION_REJECTION_MESSAGE =
  "AgentGate verifies payments against invoices only. Quotations, quotes, and estimates are pre-payment documents — issue or receive an invoice before paying.";

export const RECEIPT_REJECTION_MESSAGE =
  "This document is a receipt — proof of a payment already made. Paying it again would duplicate the payment. Verify against the unpaid invoice instead.";

export const PURCHASE_ORDER_REJECTION_MESSAGE =
  "This document is a purchase order — a commitment to buy, not a bill. Wait for the vendor's invoice, then verify the payment against it.";

export const CREDIT_NOTE_REJECTION_MESSAGE =
  "This document is a credit note or carries a negative total. Credit notes are out of scope for payment verification — process refunds through your accounts-payable workflow, not the payment gate.";

export const STATEMENT_REJECTION_MESSAGE =
  "This document is a statement of account — a summary of multiple invoices. Verify and pay the individual invoices it lists.";

export const MULTI_CURRENCY_REJECTION_MESSAGE =
  "This document shows amounts in more than one currency; AgentGate will not pick one by guessing. Verify against a single-currency invoice or route to a human.";

const MONTHS: Record<string, string> = {
  january: "01", february: "02", march: "03", april: "04",
  may: "05", june: "06", july: "07", august: "08",
  september: "09", october: "10", november: "11", december: "12",
  jan: "01", feb: "02", mar: "03", apr: "04", jun: "06",
  jul: "07", aug: "08", sep: "09", sept: "09", oct: "10",
  nov: "11", dec: "12",
};

const KNOWN_CURRENCY_CODES = new Set([
  "USD", "EUR", "GBP", "INR", "JPY", "CNY", "AUD", "CAD", "CHF", "SGD",
  "HKD", "NZD", "SEK", "NOK", "DKK", "AED", "SAR", "ZAR", "BRL", "MXN",
  "PLN",
]);

const SYMBOL_TO_CODE: Record<string, string> = {
  $: "USD", "€": "EUR", "£": "GBP", "₹": "INR", "¥": "JPY",
};

// Same invoice-marker precedence the backend schema layer uses: a real invoice
// routinely REFERENCES its originating quotation, so quote wording alone must
// not reject invoice-marked text.
const INVOICE_MARKER_RE =
  /(?:\btax\s+invoice\b)|(?:\binvoice\s*(?:#|no\.?|num(?:ber)?|id)\b)|(?:^[ \t]*invoice\b)/im;

/** Collapse separators losslessly (same rules as the backend, D59). */
function canonicalNumber(rawIn: string): string {
  const raw = rawIn.replace(/[\u00A0 ]/g, "");
  const hasDot = raw.includes(".");
  const hasComma = raw.includes(",");
  if (hasDot && hasComma) {
    if (raw.lastIndexOf(",") > raw.lastIndexOf(".")) {
      return raw.replace(/\./g, "").replace(/,/g, ".");
    }
    return raw.replace(/,/g, "");
  }
  if (hasComma) {
    const idx = raw.lastIndexOf(",");
    const head = raw.slice(0, idx);
    const tail = raw.slice(idx + 1);
    if (tail.length <= 2 && !head.includes(",")) return `${head}.${tail}`;
    return raw.replace(/,/g, "");
  }
  if ((raw.match(/\./g) ?? []).length > 1) return raw.replace(/\./g, "");
  return raw;
}

/**
 * Parse one amount cell to [two-decimal string, isNegative]. Accepts US /
 * European / Indian / NBSP-grouped separators (unambiguous forms only),
 * currency symbols and ISO codes, and accounting negatives. Throws on
 * anything ambiguous.
 */
export function parseAmount(raw: string): [string, boolean] {
  let s = raw.trim();
  let negative = false;
  if (s.startsWith("(") && s.endsWith(")")) {
    negative = true;
    s = s.slice(1, -1).trim();
  }
  s = s.replace(/\b[A-Z]{3}\b/g, (code) =>
    KNOWN_CURRENCY_CODES.has(code) ? "" : code,
  );
  s = s.replace(/[$€£₹¥]/g, "").trim();
  if (s.startsWith("-")) {
    negative = true;
    s = s.slice(1).trim();
  }
  if (s.endsWith("-")) {
    negative = true;
    s = s.slice(0, -1).trim();
  }
  const core = canonicalNumber(s);
  if (!/^\d+(\.\d{1,2})?$/.test(core)) {
    throw new Error(`Could not parse money value: ${raw}`);
  }
  const [whole, frac = ""] = core.split(".");
  return [`${whole}.${(frac + "00").slice(0, 2)}`, negative];
}

/** True when the string can be normalized into wire Money.value (D1). */
export function isValidMoneyInput(raw: string): boolean {
  try {
    return !parseAmount(raw)[1];
  } catch {
    return false;
  }
}

/** Backwards-compatible non-negative money normalization. */
export function normalizeMoney(raw: string): string {
  const [value, negative] = parseAmount(raw);
  if (negative) throw new Error(`Could not parse money value: ${raw}`);
  return value;
}

function titleCaseVendor(line: string): string {
  return line
    .trim()
    .toLowerCase()
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/** "July 14, 2026" / "Jul 14 2026" / "14 July 2026" → ISO, else null. */
function isoDateFromWords(raw: string): string | null {
  const mdy = raw.trim().match(/^([A-Za-z]{3,9})\.? (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})$/);
  if (mdy) {
    const month = MONTHS[mdy[1].toLowerCase()];
    if (month) return `${mdy[3]}-${month}-${mdy[2].padStart(2, "0")}`;
  }
  const dmy = raw.trim().match(/^(\d{1,2})(?:st|nd|rd|th)? ([A-Za-z]{3,9})\.?,? (\d{4})$/);
  if (dmy) {
    const month = MONTHS[dmy[2].toLowerCase()];
    if (month) return `${dmy[3]}-${month}-${dmy[1].padStart(2, "0")}`;
  }
  return null;
}

const DATE_LABEL_RES: RegExp[] = [
  /issue\s+date\s*[:#]?\s*(.+)/i,
  /invoice\s+date\s*[:#]?\s*(.+)/i,
  /date\s+of\s+issue\s*[:#]?\s*(.+)/i,
  /bill(?:ing)?\s+date\s*[:#]?\s*(.+)/i,
  /issued(?:\s+on)?\s*[:#]\s*(.+)/i,
  /^[ \t]*date\b(?![ \t]*due)\s*[:#]?\s*(.+)/im,
];

/**
 * Find the document's issue date. Unambiguous forms become ISO; ambiguous
 * numeric forms (12/07/2026) pass through verbatim — no check consumes the
 * date, and guessing day-vs-month order would be a lie.
 */
function extractDate(text: string): string {
  for (const labelRe of DATE_LABEL_RES) {
    const match = text.match(labelRe);
    if (!match) continue;
    const tail = match[1].trim();
    const iso = tail.match(/\d{4}-\d{2}-\d{2}/);
    if (iso) return iso[0];
    const worded = tail.match(
      /(?:[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?,?\s+\d{4})/,
    );
    if (worded) {
      const converted = isoDateFromWords(worded[0]);
      if (converted) return converted;
    }
    const numeric = tail.match(/\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}/);
    if (numeric) return numeric[0];
  }
  const bareIso = text.match(/\b\d{4}-\d{2}-\d{2}\b/);
  if (bareIso) return bareIso[0];
  throw new Error(
    "Could not find an issue date (looked for Issue Date / Invoice Date / Date of issue / Billing Date / Date).",
  );
}

const RECEIPT_RES: RegExp[] = [
  /^[ \t]*(?:payment[ \t]+)?receipt\b(?![ \t]+of\b)/im,
  /\breceipt\s*(?:#|no\.?|number)\s*[:#]?/i,
  /\bpayment\s+received\b/i,
  /\bpaid\s+in\s+full\b/i,
];

const PURCHASE_ORDER_RES: RegExp[] = [
  /\bpurchase\s+order\b/i,
  /^[ \t]*P\.?O\.?\s*(?:#|no\.?|number)\s*[:#]/im,
];

const CREDIT_NOTE_RE = /\bcredit\s+(?:note|memo)\b/i;

const STATEMENT_RES: RegExp[] = [
  /\bstatement\s+of\s+account\b/i,
  /^[ \t]*account\s+statement\b/im,
];

const QUOTATION_RES: RegExp[] = [
  /\bQUOTATION\b/i,
  /\bQuote\s*#:/i,
  /\bQuote\s+Date:/i,
  /\bQuote\s+number\b/i,
  /\bEstimate\s*#:/i,
  /\bEstimate\s+Date:/i,
  /^[ \t]*estimate\b/im,
];

/**
 * Reject non-payable money documents with a type-specific message. Credit
 * notes / purchase orders / receipts / statements reject even when invoice
 * markers are present (a PAID invoice must not be paid again); the quotation
 * check applies only when NO invoice marker exists (PRD §5b).
 */
function classifyDocument(text: string): void {
  if (CREDIT_NOTE_RE.test(text)) throw new Error(CREDIT_NOTE_REJECTION_MESSAGE);
  if (PURCHASE_ORDER_RES.some((p) => p.test(text))) {
    throw new Error(PURCHASE_ORDER_REJECTION_MESSAGE);
  }
  if (RECEIPT_RES.some((p) => p.test(text))) throw new Error(RECEIPT_REJECTION_MESSAGE);
  if (STATEMENT_RES.some((p) => p.test(text))) throw new Error(STATEMENT_REJECTION_MESSAGE);
  if (!INVOICE_MARKER_RE.test(text)) {
    if (QUOTATION_RES.some((p) => p.test(text))) {
      throw new Error(QUOTATION_REJECTION_MESSAGE);
    }
  }
}

/**
 * Resolve the document currency from ISO codes adjacent to amounts, an
 * explicit Currency label, or currency symbols. Two distinct signals reject —
 * the dual-amount ambiguity rule, applied upstream.
 */
function detectCurrency(text: string): string {
  const signals = new Set<string>();
  const label = text.match(/^[ \t]*currency\s*[:#]?\s*([A-Za-z]{3})\b/im);
  if (label && KNOWN_CURRENCY_CODES.has(label[1].toUpperCase())) {
    signals.add(label[1].toUpperCase());
  }
  for (const match of text.matchAll(/\b([A-Z]{3})\s*[$€£₹¥]?\s?\d/g)) {
    if (KNOWN_CURRENCY_CODES.has(match[1])) signals.add(match[1]);
  }
  for (const match of text.matchAll(/\d[\d.,\u00A0]*[ \t]*([A-Z]{3})\b/g)) {
    if (KNOWN_CURRENCY_CODES.has(match[1])) signals.add(match[1]);
  }
  for (const [symbol, code] of Object.entries(SYMBOL_TO_CODE)) {
    if (text.includes(symbol)) signals.add(code);
  }
  if (signals.size > 1) throw new Error(MULTI_CURRENCY_REJECTION_MESSAGE);
  return signals.size === 1 ? [...signals][0] : "USD";
}

// The first amount-shaped run on a line; callers strip percent/parenthesized
// annotations first so a rate or "(USD)" is never misread as the amount.
const LINE_AMOUNT_RE = /\(?\s*-?\s*(?:[A-Z]{3}\s*)?[$€£₹¥]?\s*\d[\d.,\u00A0]*\s*\)?/;

function stripAnnotations(lineTail: string): string {
  return lineTail
    .replace(/\([^)]*%[^)]*\)/g, " ")
    .replace(/\(\s*[A-Z]{3}\s*\)/g, " ")
    .replace(/\d+(?:[.,]\d+)?\s*%/g, " ");
}

function firstAmount(lineTail: string): string | null {
  const match = stripAnnotations(lineTail).match(LINE_AMOUNT_RE);
  return match ? match[0] : null;
}

const INVOICE_NUMBER_RES: RegExp[] = [
  /(?:tax\s+invoice|invoice|bill)\s*(?:#|no\.?|num(?:ber)?|id)\s*[:#]?\s*([A-Za-z0-9][\w./-]*)/i,
  /^[ \t]*(?:tax\s+)?invoice[ \t:#]+([A-Za-z0-9][\w./-]*)[ \t]*$/im,
];

const TABLE_HEADER_RE = /^(?:description|item|details|particulars)\b/i;

const SUMMARY_LABEL_RE =
  /^[ \t]*(?:sub\s*-?\s*total|total|amount\s+due|balance\s+due|grand\s+total|sales\s+tax|value\s+added\s+tax|vat\b|gst\b|hst\b|pst\b|igst\b|cgst\b|sgst\b|tax\b)/i;

const TAX_LABEL_RE =
  /^[ \t]*(sales\s+tax|value\s+added\s+tax|vat|gst|hst|pst|igst|cgst|sgst|tax)\b(.*)$/i;

const MONEY_CELL = "\\(?-?[$€£₹¥]?[\\d][\\d.,\\u00A0]*\\)?";

const CLASSIC_ROW_RE = new RegExp(
  `^[ \\t]*(.+?)[ \\t]{2,}(-|\\d+(?:\\.\\d+)?)[ \\t]+(-|${MONEY_CELL})[ \\t]+(${MONEY_CELL})[ \\t]*$`,
);

const STRIPE_ROW_RE =
  /^[ \t]*(.+?)[ \t]+(\d+(?:\.\d+)?)[ \t]+\$?([\d.,\u00A0]+)[ \t]+[\d.]+%[ \t]+\$?([\d.,\u00A0]+)\s*$/;

// Total labels in priority order — the most specific payable-amount labels win.
const TOTAL_LABEL_RES: RegExp[] = [
  /^[ \t]*(?:total[ \t]+)?amount[ \t]+due\b(.*)$/gim,
  /^[ \t]*total[ \t]+due\b(.*)$/gim,
  /^[ \t]*balance[ \t]+due\b(.*)$/gim,
  /^[ \t]*grand[ \t]+total\b(.*)$/gim,
  /^[ \t]*(?:total|amount)[ \t]+payable\b(.*)$/gim,
  /^[ \t]*total[ \t]+amount\b(.*)$/gim,
  /^[ \t]*total\b(?![ \t]*(?:excluding|excl))(.*)$/gim,
];

const SUBTOTAL_RE =
  /^[ \t]*(?:sub\s*-?\s*total|total[ \t]+excluding[ \t]+tax|net[ \t]+(?:total|amount))\b(.*)$/gim;

const TOTAL_NOT_FOUND_MESSAGE =
  "Could not find the payable total (looked for Amount Due / Total Due / Balance Due / Grand Total / Total Payable / Total Amount / Total).";

const NUMBER_NOT_FOUND_MESSAGE =
  "Could not find an invoice number (looked for Invoice #/No./Number/ID, Bill No., Tax Invoice, or an INVOICE <reference> title line).";

/**
 * Parse realistic plain-text money documents at payment time. Deterministic —
 * no LLM — mirrors the backend upstream parser exactly.
 */
export function parseInvoiceText(raw_text: string): ParsedInvoice {
  // PDF text layers and viewer copies can carry invisible control characters
  // (e.g. a NUL where a hyphen glyph was); strip everything but \t and \n.
  const text = raw_text
    .replace(/\r\n/g, "\n")
    .replace(/[\u0000-\u0008\u000B-\u001F\u007F]/g, "")
    .trim();
  if (!text) throw new Error("Invoice text is empty.");
  classifyDocument(text);
  if (/Invoice number/i.test(text) && /Date of issue|Amount due/i.test(text)) {
    return parseStripeStyle(text);
  }
  return parseClassic(text);
}

function lineItemKind(description: string, negative: boolean): LineItem["kind"] {
  if (negative || /\b(?:discount|rebate|promo(?:tion)?|credit)\b/i.test(description)) {
    return "discount";
  }
  if (/\b(?:shipping|freight|delivery|postage)\b/i.test(description)) return "shipping";
  if (/\b(?:tax|vat|gst)\b/i.test(description)) return "tax";
  return "charge";
}

/**
 * "20" → "0.2", "8.25" → "0.0825", "0.0" → "0" — a pure decimal-point shift on
 * the digit string (never float math), trimmed of insignificant zeros so both
 * parsers emit identical wire strings.
 */
function percentToFraction(pct: string): string | null {
  const core = pct.replace(",", ".");
  if (!/^\d+(\.\d+)?$/.test(core)) return null;
  const [whole, frac = ""] = core.split(".");
  const decimals = frac.length + 2;
  const digits = (whole + frac).padStart(decimals + 1, "0");
  const shifted = `${digits.slice(0, digits.length - decimals)}.${digits.slice(digits.length - decimals)}`;
  const trimmed = shifted
    .replace(/^0+(?=\d)/, "")
    .replace(/(\.\d*?)0+$/, "$1")
    .replace(/\.$/, "");
  return trimmed;
}

function extractTaxLines(
  lines: string[],
  tableRegion: Set<number>,
  currency: string,
): TaxLineWire[] {
  const taxLines: TaxLineWire[] = [];
  lines.forEach((line, idx) => {
    if (tableRegion.has(idx)) return;
    const match = line.match(TAX_LABEL_RE);
    if (!match) return;
    const rest = match[2];
    // "Tax ID 55-1234567" / "VAT Registration ..." are identifiers, not amounts.
    if (/^[ \t]*(?:id|reg)/i.test(rest)) return;
    // "VAT included" style annotations are informational, never additive.
    if (/includ/i.test(line)) return;
    const rateMatch = rest.match(/(\d+(?:[.,]\d+)?)\s*%/);
    const cell = firstAmount(rest);
    if (cell === null) return;
    let value: string;
    let negative: boolean;
    try {
      [value, negative] = parseAmount(cell);
    } catch {
      return;
    }
    if (negative) return;
    const entry: TaxLineWire = { amount: { value, currency } };
    if (rateMatch) {
      const rate = percentToFraction(rateMatch[1]);
      if (rate !== null) entry.rate = rate;
    }
    taxLines.push(entry);
  });
  return taxLines;
}

function extractTotal(text: string): string {
  for (const totalRe of TOTAL_LABEL_RES) {
    totalRe.lastIndex = 0;
    for (const match of text.matchAll(totalRe)) {
      const cell = firstAmount(match[1]);
      if (cell === null) continue;
      let value: string;
      let negative: boolean;
      try {
        [value, negative] = parseAmount(cell);
      } catch {
        continue;
      }
      if (negative) throw new Error(CREDIT_NOTE_REJECTION_MESSAGE);
      return value;
    }
  }
  throw new Error(TOTAL_NOT_FOUND_MESSAGE);
}

function extractSubtotal(text: string, currency: string): Money | undefined {
  SUBTOTAL_RE.lastIndex = 0;
  for (const match of text.matchAll(SUBTOTAL_RE)) {
    const cell = firstAmount(match[1]);
    if (cell === null) continue;
    try {
      const [value, negative] = parseAmount(cell);
      if (!negative) return { value, currency };
    } catch {
      continue;
    }
  }
  return undefined;
}

/** Classic fixed-width / labeled table format. */
function parseClassic(text: string): ParsedInvoice {
  const lines = text.split("\n");
  const vendor = titleCaseVendor(lines.find((l) => l.trim()) ?? "Unknown Vendor");

  let invoice_number: string | null = null;
  for (const numberRe of INVOICE_NUMBER_RES) {
    const match = text.match(numberRe);
    if (match) {
      invoice_number = match[1].replace(/[.,;:]+$/, "");
      break;
    }
  }
  if (!invoice_number) throw new Error(NUMBER_NOT_FOUND_MESSAGE);

  const date = extractDate(text);
  const currency = detectCurrency(text);

  const line_items: LineItem[] = [];
  const tableRegion = new Set<number>();
  let inTable = false;
  for (let idx = 0; idx < lines.length; idx++) {
    const line = lines[idx];
    if (!inTable) {
      if (TABLE_HEADER_RE.test(line.trim())) {
        inTable = true;
        tableRegion.add(idx);
      }
      continue;
    }
    if (/^-{4,}/.test(line) || !line.trim()) {
      tableRegion.add(idx);
      continue;
    }
    const row = line.match(CLASSIC_ROW_RE);
    let parsedRow:
      | [string, string, string, string, boolean]
      | null = null;
    if (row) {
      const [, description, qtyRaw, unitRaw, amountRaw] = row;
      try {
        const [amountValue, amountNegative] = parseAmount(amountRaw);
        const unitValue = unitRaw === "-" ? amountValue : parseAmount(unitRaw)[0];
        parsedRow = [description, qtyRaw, unitValue, amountValue, amountNegative];
      } catch {
        parsedRow = null;
      }
    }
    if (parsedRow !== null) {
      const [description, qtyRaw, unitValue, amountValue, amountNegative] = parsedRow;
      line_items.push({
        description: description.trim(),
        quantity: qtyRaw === "-" ? "1" : qtyRaw,
        unit_price: { value: unitValue, currency },
        amount: { value: amountValue, currency },
        kind: lineItemKind(description, amountNegative),
      });
      tableRegion.add(idx);
      continue;
    }
    if (SUMMARY_LABEL_RE.test(line)) break;
    tableRegion.add(idx);
  }

  const total = { value: extractTotal(text), currency };
  const tax_lines = line_items.length
    ? extractTaxLines(lines, tableRegion, currency)
    : [];

  return {
    invoice_number,
    vendor,
    date,
    currency,
    line_items,
    tax_lines,
    subtotal: extractSubtotal(text, currency),
    total,
    raw_text: text,
  };
}

/** Stripe-style billing layout (the format of most SaaS invoices). */
function parseStripeStyle(text: string): ParsedInvoice {
  const numberMatch = text.match(/Invoice number[ \t]+(.+)/i);
  if (!numberMatch) throw new Error("Could not find Invoice number.");
  // PDF text layers sometimes drop the hyphen glyph in "XXXX-0000" numbers,
  // leaving a gap (often a non-breaking space); rejoin the parts with the hyphen.
  const invoice_number = numberMatch[1].trim().replace(/\s+/g, "-");

  const date = extractDate(text);

  // Coordinate-reconstructed text keeps the two-column header, so the issuer
  // sits left of "Bill to" on one line. Viewer-copied text flattens columns —
  // fall back to the first company-looking line of the seller block above it.
  const vendorMatch = text.match(/^(.+?)[ \t]{2,}Bill to\b/im);
  let vendor = vendorMatch ? vendorMatch[1].trim() : "";
  if (!vendor) {
    const flatLines = text.split("\n").map((l) => l.trim());
    const billToIdx = flatLines.findIndex((l) => /^Bill to\b/i.test(l));
    for (let i = 0; i < billToIdx; i++) {
      const l = flatLines[i];
      if (!l) continue;
      if (/^(page \d|invoice\b|date of issue|date due|expiration\b|vat\b)/i.test(l)) continue;
      if (/^[A-Z0-9]{6,}$/.test(l)) continue;
      vendor = l;
      break;
    }
  }
  if (!vendor) throw new Error("Could not find the issuing vendor (line before 'Bill to').");

  const currency = detectCurrency(text);

  const lines = text.split("\n");
  const line_items: LineItem[] = [];
  const tableRegion = new Set<number>();
  let inTable = false;
  for (let idx = 0; idx < lines.length; idx++) {
    const line = lines[idx];
    if (!inTable) {
      if (/^Description\b/i.test(line.trim())) {
        inTable = true;
        tableRegion.add(idx);
      }
      continue;
    }
    if (/^\s*Subtotal\b/i.test(line)) break;
    const row = line.match(STRIPE_ROW_RE);
    tableRegion.add(idx);
    if (!row) continue;
    const [, description, quantity, unitRaw, amountRaw] = row;
    try {
      const [unitValue] = parseAmount(unitRaw);
      const [amountValue, amountNegative] = parseAmount(amountRaw);
      line_items.push({
        description: description.trim(),
        quantity,
        unit_price: { value: unitValue, currency },
        amount: { value: amountValue, currency },
        kind: lineItemKind(description, amountNegative),
      });
    } catch {
      continue;
    }
  }

  const total = { value: extractTotal(text), currency };
  const tax_lines = line_items.length
    ? extractTaxLines(lines, tableRegion, currency)
    : [];

  return {
    invoice_number,
    vendor,
    date,
    currency,
    line_items,
    tax_lines,
    subtotal: extractSubtotal(text, currency),
    total,
    raw_text: text,
  };
}

export interface AgentProposal {
  action_type: "approve_payment" | "reject";
  amount_value: string;
  vendor: string;
  agent_rationale: string;
}

const UNRELATED_SOURCE_TEXT =
  "Totally different document. Total Due: $999.99";

function wireAmount(raw: string): string {
  return isValidMoneyInput(raw) ? normalizeMoney(raw) : raw;
}

export function buildCallerVerifyRequest(
  parsed: ParsedInvoice,
  proposal: AgentProposal,
  options: {
    /** Demo-only: prove grounding failure with real invoice structure. */
    force_bad_grounding?: boolean;
  } = {},
): { proposed_action: unknown; source: unknown } {
  const { invoice_number, vendor, date, currency, line_items, tax_lines, subtotal, total } =
    parsed;

  const invoice: Record<string, unknown> = {
    invoice_number,
    vendor,
    date,
    currency,
    line_items,
    tax_lines,
    total,
  };
  if (subtotal) invoice.subtotal = subtotal;

  const source: Record<string, unknown> = { invoice };
  if (options.force_bad_grounding) {
    source.raw_text = UNRELATED_SOURCE_TEXT;
  } else {
    source.raw_text = parsed.raw_text;
  }

  return {
    proposed_action: {
      action_type: proposal.action_type,
      invoice_number,
      amount: { value: wireAmount(proposal.amount_value), currency },
      vendor: proposal.vendor,
      adjustments: [],
      agent_rationale: proposal.agent_rationale,
    },
    source,
  };
}

export function buildFetchVerifyRequest(
  fetchId: string,
  proposal: Omit<AgentProposal, "vendor"> & { vendor: string; invoice_number: string },
): { proposed_action: unknown; source: unknown } {
  return {
    proposed_action: {
      action_type: proposal.action_type,
      invoice_number: proposal.invoice_number,
      amount: { value: wireAmount(proposal.amount_value), currency: "USD" },
      vendor: proposal.vendor,
      adjustments: [],
      agent_rationale: proposal.agent_rationale,
    },
    source: { fetch: fetchId },
  };
}

/** Classic agent misread: $1,240.00 interpreted as $12,400.00 */
export function decimalSlipAmount(correctTotal: string): string {
  const normalized = normalizeMoney(correctTotal);
  const [whole, frac] = normalized.split(".");
  return `${whole}0.${frac}`;
}

export function defaultProposal(parsed: ParsedInvoice): AgentProposal {
  return {
    action_type: "approve_payment",
    amount_value: parsed.total.value,
    vendor: parsed.vendor,
    agent_rationale: "Payment matches invoice total and vendor.",
  };
}
