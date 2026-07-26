# AgentGate

> **AgentGate is an agent-reliability gate, not a security/trust gate.** Its honest v1 claim: it catches an **honest-but-fallible agent's mistakes** — arithmetic slips, decimal errors, LLM misreads, duplicates — at the moment of the write, and returns a machine-readable reason the agent can fix against. Caller mode **requires the original invoice text** (`source.raw_text`) alongside structured fields — **agent-forged JSON alone is rejected**. It does **NOT** detect fraud or bad business judgment. Passing AgentGate means "the action is consistent with the evidence provided," never "the payment is correct or authorized." **Fetch mode** is the stronger trust anchor: the gate resolves the invoice from an operator-controlled system of record, so the agent chooses *which* invoice but never *what it says*.

A pre-action reliability gate for AI agents. When an agent proposes an action that
touches real systems ("approve invoice INV-001 for $12,500"), AgentGate checks it
against the source evidence — caller-supplied, or fetched from an
operator-controlled system of record — and typed policies, and returns
**ALLOW / BLOCK / ESCALATE**. Blocked actions come back with a compiler-grade,
machine-readable reason so the agent can correct itself.

See `DECISIONS.md` for the design reasoning. This project is a work in progress,
built incrementally.

**Live demo:** [dashboard](https://agent-gate-gamma.vercel.app) backed by the
[API](https://agentgate-api-mvob.onrender.com/health) (free tier — the first
request after idle can take up to a minute while the backend wakes).

![The dashboard blocking a misread amount, then allowing the corrected one](docs/demo.gif)

And the same gate from the agent's side — a live model (the demo agent, the
only LLM call in the system) reads the invoices and proposes the payments
itself; the gate allows the clean one and escalates the over-policy one to a
human ([run it yourself](#development)):

![The demo agent: a live model proposes, the gate allows one payment and escalates the other to a human](docs/agent-demo.gif)

## Status

The verification core is working end to end:

- **Grounding** — when raw invoice text is supplied, the money fields the checks consume must literally appear in it, matched on parsed numeric value at **token level, never substring** (`1240` does not ground inside `INV-31240` or `$11,240.00`). Coverage over those fields feeds the decision score, and a total that does not appear in the text escalates decisively regardless of the score.
- **Deterministic checks + decision** — structural arithmetic, currency, amount-vs-total, vendor, and duplicate checks over a structured invoice, producing a real **ALLOW / BLOCK / ESCALATE** decision with a machine-readable reason and a score.
- **Frame stage** — before those content checks run, the gate confirms the action is even the thing they verify: an `approve_payment` (not a flag or a reject) against the invoice number it actually names. A wrong action type or a mismatched invoice number escalates to a human — it is never auto-"fixed," because the two readings of a mismatch (wrong evidence attached vs a typo'd action) have opposite corrections and the gate cannot tell which.
- **Retry loop** — a caller consumes a BLOCK reason, applies the fix, and resubmits until it reaches ALLOW, or routes to a human when it can't. The decision record is never rewritten by the loop.
- **Honest evaluation** — a labeled dataset (`backend/eval/dataset.jsonl`) scored as precision, recall, and false-positive rate, not "accuracy." Escalating a legitimate payment counts as a false positive (the human cost of failing closed), and the consistent-but-wrong cases the threat model excludes are reported by name as known misses. Run it with `python -m eval.harness` from `backend/`. Current numbers: precision 0.62, recall 0.73, false-positive rate 0.50, in-scope recall 1.00 — imperfect on purpose; see `DECISIONS.md` (D6, D26).
- **Typed policy** — a YAML policy (`backend/agentgate/policies/default.yaml`, shipped inside the package) adds escalation thresholds (amount ceiling, grounding-coverage score floor) within the fixed precedence; it can add escalations but never open the gate. When raw invoice text is supplied, the score reflects real grounding coverage of the fields the checks consume, and a total that does not appear in the source escalates decisively regardless of that score.
- **Agent + human-in-the-loop** — a LangGraph agent (`backend/agentgate/agent/graph.py`) proposes a payment, and on a block re-proposes using the machine-readable reason as feedback (capped by the policy); on an escalate it pauses for a human to approve or reject. The correction is value-only — the agent can only change the field the gate flagged, never smuggle in an adjustment — and a malformed model response fails closed to a human rather than crashing. A human approval routes the action but never rewrites the recorded decision.
- **Fail-closed input boundary** — input that cannot be parsed becomes a valid `escalate` decision with a `null` score via a pure factory (never a crash, never an allow), with error messages bounded so raw caller text never rides into traces or the dashboard. Every caller-supplied text field is length-capped at the schema; anything over a cap is rejected and escalates to a human. This is the contract the HTTP API sits on.
- **HTTP API** — `POST /verify` serves the gate and always answers **HTTP 200 with a decision** (verified or fail-closed): an undecodable body, a schema-invalid field, an oversized request (1 MiB cap), or an unexpected internal error all become a valid `escalate` decision — never a 5xx, never a framework 422, never an allow. Unknown fields anywhere in the request are rejected rather than silently dropped (a misspelled `adjustments` key must not turn a declared withholding into an auto-"fixed" full payment), money may be sent as JSON strings or numbers (decoded to exact decimals, floats never exist), and every decimal comes back as a JSON string so nothing re-floats it downstream. `/verify` is read-only — it reads the duplicate store, records nothing, so a dry run never burns an invoice number. Note the honest consequence: recording an approval is a deliberate post-payment **library** call (`DuplicateStore.mark_approved`), so the duplicate check is live for library callers who record after paying — and **inert over the deployed HTTP surface** (nothing over HTTP ever writes the store, so `/verify` alone can never see a duplicate) until a recording path with authentication exists; v1 has no auth, and an unauthenticated recording endpoint would let anyone poison the store and force-escalate every legitimate payment. Optional Langfuse tracing observes decisions without being able to affect them (no keys → no-op; of raw invoice text it records length only, never content).

- **Web dashboard + live demo** — a Next.js site (`frontend/`): a landing page and a live demo (`/demo`) that runs the gate on **real invoice text**, not hand-edited JSON. The demo loads `.txt` invoice fixtures — or your own pasted invoice text, dropped `.txt`, or dropped digital `.pdf` (text layer extracted in the browser with pdf.js, layout reconstructed deterministically; scans need OCR upstream) — parses them client-side, **always attaches the original text as `raw_text`**, and sends the same `POST /verify` body a production integration must use. Fetch mode is demoed against the live system-of-record record (the agent sends only an invoice identifier). Then simulate agent mistakes (decimal slip, wrong action, unrelated source text), apply the gate's machine-readable fix, and resubmit to watch a BLOCK become an ALLOW. Rendering is wire-true: the decision renders exactly as returned, score `null` renders as "not computed" (never 0), money stays the exact string the gate returned (no float math), and a network failure renders an error — never a synthesized decision. Covered by a Playwright end-to-end suite that boots the real backend and crosses real CORS, in CI on every push. A plain-language walkthrough of the demo scenarios lives in [`docs/real-world-problem.md`](docs/real-world-problem.md).
- **Enforced execution (`pay_invoice`)** — the gate is no longer only advisory. The `agentgate-mcp` server's **`pay_invoice`** tool verifies exactly like `verify_action` and, **only on allow**, executes: it mints a signed single-use decision token (stdlib HMAC-SHA256 over a canonical payload binding the *entire* verified action — deliberately not JWT; the `alg` field lives inside the signed payload and only HS256 is accepted), consumes the token, **reserves the invoice number in the duplicate store, and only then runs the executor** — so two concurrent allows on the same invoice execute exactly once, with the loser refused *before* money moves (single-use tokens alone cannot close that window: two concurrent decisions hold two different valid tokens). A replayed, expired, forged, or alg-tampered token is a typed refusal; an executor failure after reservation returns `allowed_execution_failed` with the invoice left reserved for a human — no auto-retry, no unmark API. Enforcement is honest about its preconditions: it requires `AGENTGATE_SIGNING_KEY` (≥ 32 chars, **no default ever**) and a file-backed store (`AGENTGATE_DB_PATH` — consumed tokens and reservations must survive a restart), and fail-closes instructively without them, while the advisory tools keep working in-memory and keyless. This also makes the duplicate check **live over the wire** for the first time: the first enforced payment records the invoice; every later verification of it escalates. The reference executor is a deterministic sandbox — real rails (the card-intent adapter) plug into the same executor protocol without touching the gate.
- **MCP server + pip package** — `pip install .` installs `agentgate` with the default policy shipped inside the wheel, and `agentgate-mcp` serves `pay_invoice` (enforced) and `verify_action` (advisory) over stdio for MCP-speaking agents. Both tools run the same validation and decision path as the HTTP API and always return a result — never a tool error the calling agent might route around. Money over MCP must be JSON strings: the transport parses JSON before AgentGate sees it, so a numeric amount is already a lossy float and is rejected into a fail-closed escalate. A CI job installs the package into a clean environment on every push and verifies a decision from it.
- **Document understanding, by type** — the upstream parser (`agentgate/upstream/`, mirrored exactly in the browser demo) classifies a money document before parsing it. Payable documents (invoices/bills) parse across layouts and label variants — `Invoice #/No./Number/ID`, `Bill No.`, `Tax Invoice`, `Amount/Total/Balance Due`, `Grand Total`, `Amount Payable` — with currencies detected from symbols and ISO codes (`$ € £ ₹ ¥`, `USD/EUR/GBP/INR/…`; two conflicting currencies reject as ambiguous), unambiguous international number formats (`1.240,50`, `1240,50`, `1,24,000.00`, NBSP grouping) parsed losslessly and grounded losslessly, tax summary lines (`VAT (20%)`, `Sales Tax`, `GST` — rate optional), discount and shipping line kinds, decimal quantities, and total-only documents flowing to the gate's honest "unverifiable ⇒ escalate" path. Non-payable money documents are recognized and rejected with the reason: a **quotation** is pre-payment (though an invoice merely *referencing* its quote still parses), a **receipt** is proof of a payment already made, a **purchase order** is not a bill, a **credit note** is out of scope, a **statement** is a summary of invoices to verify individually. Anything unclassifiable fails naming exactly what was looked for — never a guess, never a crash.
- **Execute layer (orchestrator, test rail)** — `POST /orchestrator/execute` runs the whole chain: parse → propose → verify → on ALLOW pay through a deterministic **test** payment rail (the only wired mode; real credentials never exist in this process) → audit. The duplicate store is reserved **before** the rail runs, so two concurrent executions of the same invoice can never both pay — the loser aborts before money moves, and a rail failure after reserve leaves the invoice number burned with a typed `allowed_execution_failed` status for a human. ESCALATE queues a pending human approval (`/orchestrator/approvals/{id}/decide`) with first-resolution-wins semantics and an expiry (default 72h); reject and expiry execute nothing.

- **Independent source fetch (fetch mode)** — instead of supplying evidence, a caller may send `"source": {"fetch": "INV-2026-0042"}` and the gate resolves the invoice from an operator-configured system of record (`AGENTGATE_RECORDS_DIR`, a directory of JSON records keyed by the invoice number *inside* each record — filenames are never trusted, so no path is ever built from caller input). Mixing `fetch` with caller-supplied evidence is rejected, every fetch failure (unknown invoice, unconfigured or corrupt store) fails closed to a human, and fetched decisions mark every evidence entry with a `system_of_record:` prefix so provenance is visible on the wire. The trust claim upgrade is real but scoped: it holds when the records directory is writable only by the operator, and the v1 reference store is a local directory — a live ERP/ledger connector is the remaining milestone.

## Quickstart

Ask the live gate to verify an action (money values are strings — exactness is the product):

```bash
curl -s -X POST https://agentgate-api-mvob.onrender.com/verify \
  -H 'Content-Type: application/json' \
  -d '{
    "proposed_action": {"action_type": "approve_payment", "invoice_number": "INV-001",
                        "amount": {"value": "12400.00", "currency": "USD"}, "vendor": "Acme Corp"},
    "source": {"invoice": {"invoice_number": "INV-001", "vendor": "Acme Corp",
               "date": "2026-01-15", "currency": "USD",
               "line_items": [{"description": "Widget", "quantity": "1",
                 "unit_price": {"value": "1240.00", "currency": "USD"},
                 "amount": {"value": "1240.00", "currency": "USD"}, "kind": "charge"}],
               "total": {"value": "1240.00", "currency": "USD"}},
               "raw_text": "INVOICE INV-001 Acme Corp Total Due: $1,240.00"}}'
```

The response is a `block` with `"field_to_change": "proposed_action.amount"` and the exact expected value — a reason an agent can fix against and resubmit.

Or let the gate fetch the evidence itself (fetch mode — the live deploy serves a demo record, `INV-2026-0042`, from its system of record):

```bash
curl -s -X POST https://agentgate-api-mvob.onrender.com/verify \
  -H 'Content-Type: application/json' \
  -d '{
    "proposed_action": {"action_type": "approve_payment", "invoice_number": "INV-2026-0042",
                        "amount": {"value": "3610.00", "currency": "USD"}, "vendor": "Acme Corp"},
    "source": {"fetch": "INV-2026-0042"}}'
```

The `allow` comes back with `"evidence_used": ["system_of_record:invoice:INV-2026-0042", "system_of_record:raw_text"]` — the caller supplied only an identifier, so the evidence provably came from the operator's records. Tamper with the amount and the block's expected value is the stored truth, not anything the caller sent. Or use it as a library:

```bash
pip install "git+https://github.com/varunk14/AgentGate.git#subdirectory=backend"
```

```python
from agentgate.core.decision import decide
from agentgate.core.schemas import Invoice, ProposedAction

decision = decide(Invoice.model_validate(invoice_dict),
                  ProposedAction.model_validate(action_dict))
print(decision.decision, [r.message for r in decision.reasons])
```

Or as MCP servers for an agent runtime (install with the `mcp` extra):

- **`agentgate-mcp`** — the core gate: **`pay_invoice`** (enforced — verify, then execute only on allow via a signed single-use token; needs `AGENTGATE_SIGNING_KEY` + `AGENTGATE_DB_PATH`) and **`verify_action`** (advisory — ALLOW / BLOCK / ESCALATE only)
- **`agentgate-upstream-mcp`** — upstream tools: **`parse_invoice`**, **`propose_payment`**, **`process_invoice`**

See [`docs/mcp-setup.md`](docs/mcp-setup.md) and [`docs/orchestrator.md`](docs/orchestrator.md) (verify → pay → audit).

## Architecture

```
proposed action  +  evidence: caller-supplied (structured invoice, optional raw
        |           text) OR fetched from the operator's system of record
        v
  frame stage          is this even an approve_payment against the invoice it
        |              names?  wrong frame -> ESCALATE, score null
        v
  deterministic        structural arithmetic, currency, amount-vs-total,
  checks               vendor, duplicate — pure functions, Decimal only
        |
        v
  grounding            do the invoice's numbers literally appear in the raw
        |              text?  token-level Decimal match, never substring
        v
  policy               amount ceiling, score floor — config can add
        |              escalations, never open the gate
        v
  ALLOW   |   BLOCK (machine-fixable reason)   |   ESCALATE (human)
```

The verification path contains **no LLM at all** — every check, the grounding
match, and the score are regex and exact-decimal arithmetic, so the gate is
deterministic, free to run, runs unmocked in CI, and the eval harness scores
the real decision function. The only model call in the entire system is the
demo agent's proposal step, behind `llm_router.py` — the only seam tests
replace with canned output. No verification logic is ever mocked when its
verdict is asserted; the remaining test doubles are fault-injection only,
forcing the catch-all handler and the tracer down failure paths nothing else
can reach. Anything the gate cannot verify fails closed to a human.

## Evaluation, honestly

Scored as interventions vs. hand-labeled ground truth over 21 cases —
escalating a legitimate payment counts as a **false positive** (the human cost
of failing closed), and the misses the threat model predicts are reported by
name, not hidden. Run it: `python -m eval.harness` from `backend/`.

| Metric | Value |
| --- | --- |
| Precision | 0.615 |
| Recall | 0.727 |
| False-positive rate | 0.500 |
| In-scope recall | 1.000 |

Known misses (out of scope by design — consistent-but-wrong *caller-supplied*
sources; fetch mode exists precisely so a deployment can take the evidence out
of the caller's hands, but the eval scores the caller-supplied path):
`oos_doctored_source`,
`oos_renumbered_double_bill`, `oos_unauthorized_spend`. The false positives
are fail-closed escalations of legitimate-but-unverifiable payments (a
declared withholding, a vendor rename, a total-only invoice, an over-threshold
amount, a reject the gate cannot judge).

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e "backend[dev]"
cd backend && pytest        # live-provider tests are excluded by default
```

Run the API locally (from `backend/`, with the `server` extra installed):

```bash
pip install -e ".[server]"
uvicorn agentgate.main:app                 # GET /health, POST /verify
```

Optional environment: `AGENTGATE_DB_PATH` points the duplicate store at a file
(default is in-memory); `AGENTGATE_RECORDS_DIR` points fetch mode at a
directory of JSON invoice records (unset = fetch requests escalate);
`AGENTGATE_CORS_ORIGINS` grants cross-origin access to
exact browser origins (unset = none); `LANGFUSE_PUBLIC_KEY` /
`LANGFUSE_SECRET_KEY` (and optionally `LANGFUSE_HOST`) enable tracing — see
`backend/.env.example`.

Watch a live model go through the gate (the demo agent — the only part of the
system that calls an LLM; needs the `agent` and `llm` extras plus a provider
key in the environment):

```bash
pip install -e ".[agent,llm]"
python -m agentgate.agent.demo       # reads GEMINI_API_KEY (default model)
```

Two scenarios run (recorded in the second GIF at the top of this README): a
clean invoice the model reads and the gate verifies (a misread comes back as
a machine-fixable block the agent corrects and resubmits), then an invoice
over the policy amount ceiling, which escalates and pauses for you to approve
or reject. The narration reports whatever actually happened — nothing is
scripted.

Run the dashboard (from `frontend/`):

```bash
npm install
npm run dev                          # expects the API on http://127.0.0.1:8000
npm run e2e                          # Playwright gate: boots backend + frontend itself
```

Point it at a different API with `NEXT_PUBLIC_AGENTGATE_API`; set
`NEXT_PUBLIC_TRACE_URL_TEMPLATE` (a URL containing `{id}`) to turn trace ids
into links.

## Deploy

### Backend on Fly.io (recommended)

From `backend/`:

```bash
fly auth login
fly launch --no-deploy    # first time only; accept app name agentgate-api
fly secrets set AGENTGATE_CORS_ORIGINS="https://your-frontend.vercel.app,http://127.0.0.1:3000,http://localhost:3000"
fly deploy
```

The API will be at `https://agentgate-api.fly.dev` (or the name you chose).
Health check: `GET /health`. Fetch mode uses the demo records baked into the
image (`AGENTGATE_RECORDS_DIR=data/system_of_record` in `fly.toml`).

### Frontend

Copy `frontend/.env.example` to `frontend/.env.local` and set:

```bash
NEXT_PUBLIC_AGENTGATE_API=https://agentgate-api.fly.dev
```

Restart `npm run dev` (or set the same variable in Vercel for production).
The backend must list your frontend origin in `AGENTGATE_CORS_ORIGINS` or the
browser will block cross-origin calls.

### Backend on Render (alternative)

The backend also deploys to Render from `render.yaml` (free plan; ~1 minute
cold start after idle, ephemeral disk). Order matters: bring up the backend,
point the frontend at that URL, then set that frontend origin in
`AGENTGATE_CORS_ORIGINS`.

## License

MIT — see `LICENSE`.

