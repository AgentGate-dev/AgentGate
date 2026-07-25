import { test, expect } from "@playwright/test";
import fs from "fs";
import path from "path";
import { INVOICE_FIXTURES } from "../lib/invoices";

// ORDER MATTERS. One backend with in-memory stores serves the whole file
// (fullyParallel: false), and the auto "Verify & execute" path PAYS — it burns
// the invoice number in the duplicate store. So: read-only /verify scenarios
// on an invoice run BEFORE the test that pays it, every paying test uses a
// distinct invoice number, and the duplicate-protection test deliberately
// re-pays a number an earlier test burned.

const ACME = path.join(__dirname, "..", "public", INVOICE_FIXTURES.acme.slice(1));
const NORTHWIND = path.join(__dirname, "..", "public", INVOICE_FIXTURES.northwind.slice(1));

const EURO_INVOICE = `NORDWIND GMBH
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
`;

const RECEIPT_TEXT = `PAYMENT RECEIPT

Receipt #: R-1001
Invoice #: INV-42
Paid in full: $100.00
Thank you for your payment.
`;

async function uploadInvoice(page: import("@playwright/test").Page, filePath: string) {
  await page.setInputFiles('[data-testid="invoice-upload"]', filePath);
  await expect(page.getByTestId("verify")).toBeEnabled();
}

async function pasteInvoice(page: import("@playwright/test").Page, text: string) {
  await page.getByTestId("invoice-paste").fill(text);
  await page.getByTestId("invoice-paste").blur();
}

// Fault-injection toggles and fetch mode live in the collapsed developer panel.
async function openDeveloperPanel(page: import("@playwright/test").Page) {
  await page.locator('[data-testid="developer-scenarios"] summary').click();
}

test("the home page presents the enterprise product landing", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByTestId("landing-page")).toBeVisible();
  await expect(page.getByTestId("hero-demo-cta")).toBeVisible();
  await expect(page.getByTestId("plan-self-host")).toBeVisible();
  await expect(page.getByTestId("home-try-demo")).toBeVisible();
});

test("the demo starts empty until the user provides an invoice", async ({ page }) => {
  await page.goto("/demo");
  await expect(page.getByTestId("evidence-empty")).toBeVisible();
  await expect(page.getByTestId("verify")).toHaveCount(0);
});

// --- read-only /verify scenarios on the Acme invoice (must precede the test
// --- that PAYS Acme further down) --------------------------------------------

test("a real invoice decimal slip blocks then fixes on resubmit", async ({ page }) => {
  await page.goto("/demo?mistake=decimal");
  await uploadInvoice(page, ACME);
  await expect(page.getByTestId("decimal-slip")).toBeChecked();
  await page.click('[data-testid="verify"]');
  await expect(page.getByTestId("decision-banner")).toHaveText("block");
  const reasons = page.getByTestId("reasons");
  await expect(reasons).toContainText("action_amount_matches_total");
  await expect(reasons).toContainText("1240.00 USD");
  await page.click('[data-testid="apply-fix"]');
  await expect(page.getByTestId("decision-banner")).toHaveText("allow");
});

test("a reject action on a real invoice escalates", async ({ page }) => {
  await page.goto("/demo");
  await uploadInvoice(page, ACME);
  await openDeveloperPanel(page);
  await page.selectOption('[data-testid="proposal-action-type"]', "reject");
  await page.click('[data-testid="verify"]');
  await expect(page.getByTestId("decision-banner")).toHaveText("escalate");
  await expect(page.getByTestId("score")).toHaveText("not computed");
});

test("unrelated source text on a real invoice escalates grounding", async ({ page }) => {
  await page.goto("/demo");
  await uploadInvoice(page, ACME);
  await openDeveloperPanel(page);
  await page.click('[data-testid="bad-grounding"]');
  await page.click('[data-testid="verify"]');
  await expect(page.getByTestId("decision-banner")).toHaveText("escalate");
  await expect(page.getByTestId("reasons")).toContainText("total_not_grounded");
});

// --- execute path: pays, burns invoice numbers ---------------------------------

test("a clean Acme invoice verifies and executes a test payment", async ({ page }) => {
  await page.goto("/demo");
  await uploadInvoice(page, ACME);
  await page.click('[data-testid="verify"]');
  await expect(page.getByTestId("decision-banner")).toHaveText("allow");
  await expect(page.getByTestId("score")).toHaveText("1.00");
  await expect(page.locator('[data-testid="checks-table"] tbody tr')).toHaveCount(7);
  await expect(page.getByTestId("execution-status")).toHaveText("paid");
  await expect(page.getByTestId("payment-execution-panel")).toContainText("pay_test_");
});

test("re-submitting a paid invoice hits duplicate protection", async ({ page }) => {
  // Depends on the previous test having paid INV-001 (same file, serial order).
  await page.goto("/demo");
  await uploadInvoice(page, ACME);
  await page.click('[data-testid="verify"]');
  await expect(page.getByTestId("decision-banner")).toHaveText("escalate");
  await expect(page.getByTestId("reasons")).toContainText("duplicate_check");
  await expect(page.getByTestId("execution-status")).toHaveText("pending_human_approval");
  await page.click('[data-testid="reject-escalation"]');
  await expect(page.getByTestId("execution-status")).toHaveText("rejected_by_human");
});

test("a $12,500 invoice escalates on policy, then a human approves and pays", async ({ page }) => {
  await page.goto("/demo");
  await uploadInvoice(page, NORTHWIND);
  await page.click('[data-testid="verify"]');
  await expect(page.getByTestId("decision-banner")).toHaveText("escalate");
  await expect(page.getByTestId("execution-status")).toHaveText("pending_human_approval");
  await page.click('[data-testid="approve-escalation"]');
  await expect(page.getByTestId("execution-status")).toHaveText("paid_after_human_approval");
  await expect(page.getByTestId("payment-execution-panel")).toContainText("pay_test_");
});

// --- document understanding breadth --------------------------------------------

test("a European invoice (EUR, decimal-comma amounts) parses, verifies, and pays", async ({ page }) => {
  await page.goto("/demo");
  await pasteInvoice(page, EURO_INVOICE);
  await expect(page.getByTestId("proposal-amount")).toHaveValue("1240.50");
  await page.click('[data-testid="verify"]');
  await expect(page.getByTestId("decision-banner")).toHaveText("allow");
  await expect(page.getByTestId("execution-status")).toHaveText("paid");
});

test("a receipt is recognized and rejected as already paid", async ({ page }) => {
  await page.goto("/demo");
  await pasteInvoice(page, RECEIPT_TEXT);
  await expect(page.getByTestId("invoice-load-error")).toContainText("receipt");
});

test("fetch mode uses the live system-of-record record", async ({ page }) => {
  await page.goto("/demo");
  await openDeveloperPanel(page);
  await page.getByTestId("fetch-id-input").fill("INV-2026-0042");
  await page.click('[data-testid="load-fetch"]');
  await page.getByTestId("proposal-amount").fill("3610.00");
  await page.getByTestId("proposal-vendor").fill("Acme Corp");
  await page.click('[data-testid="verify"]');
  await expect(page.getByTestId("decision-banner")).toHaveText("allow");
  await expect(page.getByTestId("score")).toHaveText("1.00");
  await expect(page.locator("dd").filter({ hasText: "system_of_record" })).toBeVisible();
});

test("uploading a PDF invoice extracts its text layer, parses, and verifies", async ({ page }) => {
  await page.goto("/demo");
  await page.setInputFiles(
    '[data-testid="invoice-upload"]',
    path.join(__dirname, "fixtures", "brightpath-inv-2026-0788.pdf"),
  );
  await expect(page.getByTestId("verify")).toBeEnabled();
  await expect(page.getByTestId("proposal-amount")).toHaveValue("6200.00");
  await page.click('[data-testid="verify"]');
  await expect(page.getByTestId("decision-banner")).toHaveText("allow");
});

test("a Stripe-style PDF invoice (Invoice number / Amount due layout) parses and verifies", async ({ page }) => {
  await page.goto("/demo");
  await page.setInputFiles(
    '[data-testid="invoice-upload"]',
    path.join(__dirname, "fixtures", "meridian-stripe-style.pdf"),
  );
  await expect(page.getByTestId("verify")).toBeEnabled();
  await expect(page.getByTestId("proposal-amount")).toHaveValue("84.00");
  await page.click('[data-testid="verify"]');
  await expect(page.getByTestId("decision-banner")).toHaveText("allow");
});

test("pasting viewer-copied Stripe-style invoice text parses the flattened columns", async ({ page }) => {
  // Parse-only: this text carries the same invoice number the PDF test above
  // already paid, so a verify here would (correctly) hit duplicate protection.
  const copied = fs.readFileSync(
    path.join(__dirname, "fixtures", "meridian-viewer-copy.txt"),
    "utf8",
  );
  await page.goto("/demo");
  await pasteInvoice(page, copied);
  await expect(page.getByTestId("proposal-amount")).toHaveValue("84.00");
});

test("a quotation is rejected at upload — payments verify against invoices only", async ({ page }) => {
  await page.goto("/demo");
  await page.setInputFiles(
    '[data-testid="invoice-upload"]',
    path.join(__dirname, "fixtures", "brightpath-quotation.txt"),
  );
  await expect(page.getByTestId("invoice-load-error")).toContainText("invoices only");
});

test("/verify redirects to the live demo", async ({ page }) => {
  await page.goto("/verify");
  await expect(page).toHaveURL(/\/demo$/);
});
