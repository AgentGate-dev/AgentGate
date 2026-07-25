# AgentGate: The Real Problem and How It Works in Real Time

This document explains AgentGate in plain language — what breaks in the real world, what the product does at decision time, and how the live demo uses **real invoice files** (not dummy JSON).

---

## The real problem

AI agents are now allowed to **propose payments**. Most stacks have no checkpoint between:

> “The model said pay this”  
> and  
> “Money actually moved”

### A realistic failure

1. An agent reads an invoice: **Acme Corp, INV-001, $1,240.00**
2. It proposes: **approve $12,400.00** (decimal/comma slip)
3. Automation pays the wrong amount
4. Finance finds out after the money is gone

That is usually **not fraud** — it is an honest agent mistake.

**AgentGate** sits before execution and returns **ALLOW / BLOCK / ESCALATE** with a machine-readable reason.

---

## What happens in real time

```text
Real invoice (email / ERP / .txt)
        ↓
Your agent reads & proposes approve_payment
        ↓
AgentGate verifies (milliseconds, no LLM)
        ↓
ALLOW  |  BLOCK (fixable)  |  ESCALATE (human)
```

The verification path uses **Decimal arithmetic and rules only** — not model confidence.

---

## Scenario A — decimal slip (real Acme invoice)

**Evidence:** `frontend/public/invoices/acme-inv-001.txt` (same text as `backend/data/sample_invoices/acme_good.txt`)

1. Invoice total is **$1,240.00** (Widget A + Widget B + shipping)
2. Agent proposes **$12,400.00**
3. Gate **BLOCK** — expected `1240.00`, field `proposed_action.amount`
4. Agent fixes amount and resubmits
5. Gate **ALLOW**

---

## Scenario B — policy ceiling (real Northwind invoice)

**Evidence:** `frontend/public/invoices/northwind-inv-12500.txt`

1. Invoice total is **$12,500.00** (correctly read)
2. All evidence checks pass
3. Policy: amount > **$10,000** → **ESCALATE**
4. Human approves or rejects — nothing auto-pays on escalate alone

---

## Scenario C — fetch mode (real ERP record)

**Evidence:** loaded by the gate from `backend/data/system_of_record/acme-widgets-q1.json`

1. Agent sends only `"source": {"fetch": "INV-2026-0042"}`
2. Gate loads the operator’s record (**$3,610.00** truth)
3. Tampered amounts **BLOCK** against stored truth, not caller-supplied fiction

Hosted sandbox: set `AGENTGATE_RECORDS_DIR=data/system_of_record` on the API (already in `render.yaml`).

---

## Why the demo used to show JSON (and what we do now)

| Layer | Job |
|-------|-----|
| **Upstream (agent / OCR / ERP)** | Read PDF/email → structured invoice + optional raw text |
| **AgentGate** | Verify proposed payment against that evidence |
| **Orchestrator** | Execute payment only on ALLOW (or human-approved escalate) |

The API always speaks JSON — that is the **wire contract**. But the demo no longer ships **canned dummy payloads**, and the gate **rejects JSON-only evidence**:

1. Loads **real `.txt` invoices** from `public/invoices/`
2. **Parses** them deterministically into structured fields + **`raw_text` (always attached)**
3. Builds the same `POST /verify` body production uses — caller mode **requires** `source.raw_text`; use **`fetch`** when the agent must not supply the invoice body
4. Lets you simulate agent mistakes (decimal slip, wrong action, bad grounding)
5. Supports **drag-and-drop** of your own invoice `.txt` or `.pdf`

**Enterprise rule:** nobody verifies by handing the gate structured JSON alone. Either attach the original invoice text, or send only a fetch identifier and let the gate load truth from your system of record.

Digital PDFs are read in the browser: pdf.js extracts the text layer and the layout (line breaks, column spacing) is reconstructed deterministically from glyph coordinates — no OCR, no LLM — then fed through the same parser as `.txt`. The parser recognizes two **invoice** layouts: the classic fixed-width table (the fixtures above) and the Stripe-style billing layout (`Invoice number` / `Date of issue` / `Amount due`) that most SaaS invoices use. **Quotations, quotes, and estimates are rejected** — payment verification belongs at invoice time, not quote time. This is still the *upstream* layer simulated client-side; the gate only ever sees the JSON body. Scanned (image-only) PDFs are rejected with an instructive error: OCR belongs upstream of the gate — paste the extracted text instead.

---

## Try it

```bash
cd frontend && npm run dev
```

Open **`/demo`**:

- Pick **Acme Corp — widget order** (real file)
- Toggle **Simulate agent decimal slip** → Run verification → **BLOCK**
- Click **Apply gate fix & resubmit** → **ALLOW**
- Pick **Northwind — platform license** → **ESCALATE** (policy)
- Pick **Acme widgets Q1 (fetch mode)** → **ALLOW** with `system_of_record:` evidence

---

## One-line summary

**AgentGate verifies agent payment proposals against real invoice evidence in real time — before money moves.**

---

## Related

- [README](../README.md) — API, architecture, threat model
- Live demo: `/demo`
