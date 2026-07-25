# Enterprise execute layer (orchestrator)

The **gate** verifies; the **orchestrator** pays (on ALLOW) and audits. This is the
module enterprises buy alongside the core gate.

## Flow

```text
Invoice text
    → upstream parse + propose
    → gate (allow | block | escalate)
    → on ALLOW: reserve invoice number → test payment rail → audit log
    → on ESCALATE: human approval queue → pay only if approved
```

Money-safety semantics:

- **Reserve before pay.** The duplicate store is marked *before* the payment
  rail runs, so two concurrent executions of the same invoice can never both
  pay — the loser aborts with `payment_aborted_duplicate` before money moves.
- **`allowed_execution_failed`** — the rail failed *after* the reserve. The
  invoice number stays burned (unknown outcome = a human investigates;
  unmarking would reopen the double-pay window).
- **First resolution wins.** An approval or rejection is a conditional update;
  a second decision on the same pending approval is refused.
- **`approval_expired`** — pending approvals expire (default 72h); an expired
  approval executes nothing.

## HTTP API

| Endpoint | Purpose |
|----------|---------|
| `POST /orchestrator/execute` | Full flow: `{ "raw_text": "..." }` |
| `POST /orchestrator/approvals/{id}/decide` | `{ "approve": true \| false }` |
| `GET /orchestrator/audit` | Recent audit events |

## Environment

- `AGENTGATE_PAYMENT_MODE=test` — default and the **only** wired mode; fake
  payments, no network, no credentials. Any other value refuses at startup.
- `AGENTGATE_ORCHESTRATOR_DB_PATH` — audit + approval queue (default `:memory:`)
- `AGENTGATE_DB_PATH` — duplicate store (shared with gate)
- `AGENTGATE_APPROVAL_TTL_HOURS` — pending-approval expiry (default `72`)

## Demo

On http://127.0.0.1:3000/demo click **Verify & execute (test payment)**.

- **Acme invoice** → ALLOW → `execution.status: paid` + `pay_test_...` id
- **Northwind $12.5k** → ESCALATE → **Human approve & pay** or **Reject**

## Production path

Replace `TestPaymentProvider` with Stripe / ERP connectors in
`agentgate/orchestrator/payment.py` — the gate stays unchanged.
