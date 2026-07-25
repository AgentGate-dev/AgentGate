"use client";

import { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import {
  buildCallerVerifyRequest,
  buildFetchVerifyRequest,
  decimalSlipAmount,
  defaultProposal,
  isValidMoneyInput,
  parseInvoiceText,
  type AgentProposal,
  type ParsedInvoice,
} from "../lib/invoice-parser";
import type { BlockReason, Decision, Money } from "../lib/types";

const API_BASE =
  process.env.NEXT_PUBLIC_AGENTGATE_API ?? "http://127.0.0.1:8000";
const TRACE_URL_TEMPLATE = process.env.NEXT_PUBLIC_TRACE_URL_TEMPLATE;

function moneyOrText(v: Money | string | null): string {
  if (v === null) return "";
  if (typeof v === "string") return v;
  return `${v.value} ${v.currency}`;
}

type ExecutionPayment = {
  payment_id: string;
  status: string;
  mode: string;
  amount_value: string;
  amount_currency: string;
  vendor: string;
  invoice_number: string;
};

function executionPayment(execution: Record<string, unknown> | null): ExecutionPayment | null {
  if (!execution || typeof execution.payment !== "object" || execution.payment === null) {
    return null;
  }
  const payment = execution.payment as Record<string, unknown>;
  if (typeof payment.payment_id !== "string") return null;
  return {
    payment_id: payment.payment_id,
    status: String(payment.status ?? ""),
    mode: String(payment.mode ?? "test"),
    amount_value: String(payment.amount_value ?? ""),
    amount_currency: String(payment.amount_currency ?? ""),
    vendor: String(payment.vendor ?? ""),
    invoice_number: String(payment.invoice_number ?? ""),
  };
}

const BANNER_STYLES: Record<Decision["decision"], string> = {
  allow: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  block: "bg-red-500/15 text-red-300 border-red-500/40",
  escalate: "bg-amber-500/15 text-amber-300 border-amber-500/40",
};

function ReasonCard({ reason }: { reason: BlockReason }) {
  return (
    <li className="rounded-xl border border-white/10 bg-zinc-900/80 p-4 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-zinc-100">{reason.check}</span>
        {reason.block_type && (
          <span className="rounded-full bg-white/5 px-2 py-0.5 font-mono text-xs text-zinc-400">
            {reason.block_type}
          </span>
        )}
      </div>
      <p className="mt-2 text-zinc-400">{reason.message}</p>
      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 font-mono text-xs text-zinc-500">
        {reason.expected !== null && (
          <>
            <dt>expected</dt>
            <dd className="text-zinc-200">{moneyOrText(reason.expected)}</dd>
          </>
        )}
        {reason.received !== null && (
          <>
            <dt>received</dt>
            <dd className="text-zinc-200">{moneyOrText(reason.received)}</dd>
          </>
        )}
        {reason.field_to_change && (
          <>
            <dt>field_to_change</dt>
            <dd className="text-zinc-200">{reason.field_to_change}</dd>
          </>
        )}
      </dl>
    </li>
  );
}

function VerifyDashboardInner() {
  const searchParams = useSearchParams();
  const [rawText, setRawText] = useState<string | null>(null);
  const [parsed, setParsed] = useState<ParsedInvoice | null>(null);
  const [fetchId, setFetchId] = useState<string | null>(null);
  const [fetchInput, setFetchInput] = useState("");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [proposal, setProposal] = useState<AgentProposal | null>(null);
  const [badGrounding, setBadGrounding] = useState(false);
  const [decimalSlip, setDecimalSlip] = useState(false);
  const [decision, setDecision] = useState<Decision | null>(null);
  const [execution, setExecution] = useState<Record<string, unknown> | null>(null);
  const [auditId, setAuditId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [attempt, setAttempt] = useState(1);

  const resetDecision = useCallback(() => {
    setAttempt(1);
    setDecision(null);
    setExecution(null);
    setAuditId(null);
    setError(null);
  }, []);

  const loadFromText = useCallback(
    (text: string) => {
      setLoadError(null);
      setFetchId(null);
      setFetchInput("");
      setRawText(text);
      try {
        const next = parseInvoiceText(text);
        setParsed(next);
        setProposal(defaultProposal(next));
        resetDecision();
      } catch (err) {
        setParsed(null);
        setProposal(null);
        setLoadError(String(err));
      }
    },
    [resetDecision],
  );

  const startFetchMode = useCallback(() => {
    const id = fetchInput.trim();
    if (!id) {
      setLoadError("Enter an invoice number to resolve from your system of record.");
      return;
    }
    setLoadError(null);
    setParsed(null);
    setFetchId(id);
    setProposal({
      action_type: "approve_payment",
      amount_value: "",
      vendor: "",
      agent_rationale: "Agent proposes payment against a fetched invoice record.",
    });
    resetDecision();
  }, [fetchInput, resetDecision]);

  useEffect(() => {
    if (searchParams.get("mistake") === "decimal") setDecimalSlip(true);
  }, [searchParams]);

  useEffect(() => {
    if (!proposal || !parsed) return;
    if (!decimalSlip) {
      setProposal((p) =>
        p ? { ...p, amount_value: parsed.total.value, vendor: parsed.vendor } : p,
      );
      return;
    }
    setProposal((p) =>
      p
        ? {
            ...p,
            amount_value: decimalSlipAmount(parsed.total.value),
            agent_rationale: "Agent misread comma grouping in total.",
          }
        : p,
    );
  }, [decimalSlip, parsed]);

  const requestBody = useMemo(() => {
    if (!proposal) return "";
    try {
      if (fetchId) {
        return JSON.stringify(
          buildFetchVerifyRequest(fetchId, {
            ...proposal,
            invoice_number: fetchId,
            vendor: proposal.vendor,
          }),
          null,
          2,
        );
      }
      if (parsed) {
        return JSON.stringify(
          buildCallerVerifyRequest(parsed, proposal, {
            force_bad_grounding: badGrounding,
          }),
          null,
          2,
        );
      }
      return "";
    } catch {
      return "";
    }
  }, [fetchId, parsed, proposal, badGrounding]);

  const amountMissing =
    proposal !== null && !isValidMoneyInput(proposal.amount_value);

  const usesManualPath =
    fetchId !== null ||
    badGrounding ||
    decimalSlip ||
    (proposal?.action_type ?? "approve_payment") !== "approve_payment";

  async function runOrchestratorExecute() {
    if (!rawText?.trim()) return;
    setLoading(true);
    setError(null);
    setDecision(null);
    setExecution(null);
    setAuditId(null);
    try {
      const resp = await fetch(`${API_BASE}/orchestrator/execute`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ raw_text: rawText }),
      });
      if (!resp.ok) {
        setError(`The API returned HTTP ${resp.status}. Orchestrator did not complete.`);
        return;
      }
      const payload = (await resp.json()) as {
        decision: Decision;
        execution?: Record<string, unknown>;
        audit_id?: string;
      };
      setDecision(payload.decision);
      setExecution(payload.execution ?? null);
      setAuditId(payload.audit_id ?? null);
    } catch (err) {
      setError(`Could not reach the orchestrator (${String(err)}).`);
    } finally {
      setLoading(false);
    }
  }

  async function decideApproval(approve: boolean) {
    const approvalId = execution?.approval_id;
    if (typeof approvalId !== "string") return;
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch(`${API_BASE}/orchestrator/approvals/${approvalId}/decide`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ approve }),
      });
      if (!resp.ok) {
        setError(`Approval request failed with HTTP ${resp.status}.`);
        return;
      }
      const payload = (await resp.json()) as {
        decision?: Decision;
        execution?: Record<string, unknown>;
        audit_id?: string;
      };
      if (payload.decision) setDecision(payload.decision);
      if (payload.execution) setExecution(payload.execution);
      if (payload.audit_id) setAuditId(payload.audit_id);
    } catch (err) {
      setError(`Could not submit approval (${String(err)}).`);
    } finally {
      setLoading(false);
    }
  }

  async function verify(body?: string) {
    if (!usesManualPath && rawText && parsed && !fetchId) {
      await runOrchestratorExecute();
      return;
    }

    const payload = body ?? requestBody;
    if (!payload.trim()) return;
    if (proposal && !isValidMoneyInput(proposal.amount_value)) {
      setError("Enter a payment amount before running verification.");
      return;
    }

    setLoading(true);
    setError(null);
    setDecision(null);
    try {
      const resp = await fetch(`${API_BASE}/verify`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: payload,
      });
      if (!resp.ok) {
        setError(`The API returned HTTP ${resp.status}. No verification decision was produced.`);
        return;
      }
      setDecision((await resp.json()) as Decision);
    } catch (err) {
      setError(
        `Could not reach the verification API (${String(err)}). ` +
          "Hosted sandbox may need up to a minute to wake from idle.",
      );
    } finally {
      setLoading(false);
    }
  }

  function applyFixAndResubmit() {
    if (!decision || !proposal) return;
    const reason = decision.reasons.find(
      (r) =>
        r.block_type === "agent_fixable" && r.field_to_change === "proposed_action.amount",
    );
    if (!reason || reason.expected === null) return;

    const expected =
      typeof reason.expected === "string"
        ? reason.expected.split(" ")[0]
        : reason.expected.value;

    const fixed: AgentProposal = {
      ...proposal,
      amount_value: expected,
      agent_rationale: "Corrected amount from gate feedback.",
    };

    setProposal(fixed);
    setDecimalSlip(false);
    setAttempt((n) => n + 1);

    if (parsed) {
      void verify(
        JSON.stringify(
          buildCallerVerifyRequest(parsed, fixed, {
            force_bad_grounding: badGrounding,
          }),
          null,
          2,
        ),
      );
      return;
    }

    if (fetchId) {
      void verify(
        JSON.stringify(
          buildFetchVerifyRequest(fetchId, {
            ...fixed,
            invoice_number: fetchId,
            vendor: fixed.vendor,
          }),
          null,
          2,
        ),
      );
    }
  }

  async function onDropFile(file: File) {
    if (/\.pdf$/i.test(file.name) || file.type === "application/pdf") {
      setLoadError(null);
      try {
        const { extractPdfText } = await import("../lib/pdf-text");
        loadFromText(await extractPdfText(await file.arrayBuffer()));
      } catch (err) {
        setLoadError(String(err));
      }
      return;
    }
    if (!file.name.match(/\.(txt|text)$/i) && file.type && !file.type.includes("text")) {
      setLoadError("Drop a plain-text invoice (.txt) or a digital PDF with a text layer.");
      return;
    }
    loadFromText(await file.text());
  }

  const traceUrl =
    decision?.trace_id && TRACE_URL_TEMPLATE
      ? TRACE_URL_TEMPLATE.replace("{id}", decision.trace_id)
      : null;

  const canFix =
    decision?.decision === "block" &&
    decision.reasons.some(
      (r) => r.block_type === "agent_fixable" && r.field_to_change === "proposed_action.amount",
    );

  const hasEvidence = parsed !== null || fetchId !== null;
  const canRunAgent = Boolean(rawText?.trim() && parsed && !fetchId && !usesManualPath);
  const canRunManual = Boolean(requestBody && !amountMissing);

  return (
    <div data-testid="verify-dashboard">
      <header className="border-b border-white/10 pb-8">
        <p className="text-sm font-medium uppercase tracking-wider text-violet-400">Live verification</p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight text-white">
          Agent verifies your invoice through the gate
        </h1>
        <p className="mt-3 max-w-3xl text-sm leading-relaxed text-zinc-400">
          Drop or paste a real invoice — the upstream agent parses and proposes payment, the
          gate verifies, and the orchestrator executes a <span className="font-mono text-zinc-300">test</span>{" "}
          payment on ALLOW. Core outcome:{" "}
          <span className="font-mono text-zinc-300">allow</span> /{" "}
          <span className="font-mono text-zinc-300">block</span> /{" "}
          <span className="font-mono text-zinc-300">escalate</span> — then pay when allowed.
        </p>
      </header>

      <section className="mt-8">
        <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">1 · Provide invoice evidence</h2>

        <div
          data-testid="invoice-dropzone"
          onDragOver={(e) => e.preventDefault()}
          onDrop={(e) => {
            e.preventDefault();
            const file = e.dataTransfer.files[0];
            if (file) void onDropFile(file);
          }}
          className="mt-4 rounded-xl border border-dashed border-white/15 bg-zinc-950/50 p-6 text-center"
        >
          <p className="text-sm text-zinc-300">Drag and drop your invoice (.txt or digital .pdf)</p>
          <p className="mt-1 text-xs text-zinc-500">
            Invoices and bills parse at payment time — quotes, receipts, POs, credit notes are
            recognized and rejected with the reason · scanned PDFs need OCR upstream
          </p>
          <label className="mt-3 inline-block cursor-pointer text-xs text-violet-300 hover:text-violet-200">
            Browse file
            <input
              type="file"
              accept=".txt,text/plain,.pdf,application/pdf"
              data-testid="invoice-upload"
              className="hidden"
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) void onDropFile(file);
              }}
            />
          </label>
        </div>

        <textarea
          data-testid="invoice-paste"
          placeholder="Or paste invoice email / PDF-extracted text here…"
          className="mt-3 h-32 w-full rounded-xl border border-white/10 bg-zinc-950 p-3 font-mono text-xs text-zinc-300 outline-none focus:border-violet-500/50"
          onBlur={(e) => {
            if (e.target.value.trim()) loadFromText(e.target.value);
          }}
        />

        <div className="mt-6 rounded-xl border border-white/10 bg-zinc-900/40 p-4">
          <details data-testid="developer-scenarios">
            <summary className="cursor-pointer text-sm font-medium text-zinc-300">
              Developer scenarios (fetch mode, manual proposal, fault injection)
            </summary>
            <p className="mt-3 text-xs text-zinc-500">
              Production agents compose upstream MCP tools (
              <span className="font-mono">parse_invoice</span>,{" "}
              <span className="font-mono">propose_payment</span>) then call core{" "}
              <span className="font-mono">verify_action</span>. Use this panel only to simulate
              edge cases.
            </p>
            <div className="mt-4 space-y-4">
              <p className="text-sm font-medium text-white">System-of-record fetch</p>
              <p className="text-xs text-zinc-500">
                Send only an invoice identifier — the gate loads truth from{" "}
                <code className="text-zinc-400">AGENTGATE_RECORDS_DIR</code>.
              </p>
              <div className="flex flex-wrap gap-2">
                <input
                  data-testid="fetch-id-input"
                  value={fetchInput}
                  onChange={(e) => setFetchInput(e.target.value)}
                  placeholder="Invoice number, e.g. INV-2026-0042"
                  className="min-w-[16rem] flex-1 rounded-lg border border-white/10 bg-zinc-950 px-3 py-2 font-mono text-sm text-white outline-none focus:border-violet-500/50"
                />
                <button
                  type="button"
                  data-testid="load-fetch"
                  onClick={startFetchMode}
                  className="rounded-lg border border-violet-500/40 px-4 py-2 text-sm text-violet-200 hover:bg-violet-500/10"
                >
                  Use fetch mode
                </button>
              </div>

              {parsed && proposal && (
                <div className="rounded-lg border border-white/5 bg-zinc-950/50 p-3">
                  <p className="text-xs font-medium text-zinc-400">Fault injection</p>
                  <label className="mt-3 flex items-center gap-2 text-xs text-zinc-400">
                    <input
                      type="checkbox"
                      data-testid="bad-grounding"
                      checked={badGrounding}
                      onChange={(e) => setBadGrounding(e.target.checked)}
                    />
                    Attach unrelated source text (grounding failure demo)
                  </label>
                  <label className="mt-2 flex items-center gap-2 text-xs text-zinc-400">
                    <input
                      type="checkbox"
                      data-testid="decimal-slip"
                      checked={decimalSlip}
                      onChange={(e) => setDecimalSlip(e.target.checked)}
                    />
                    Simulate agent decimal slip (e.g. $1,240 → $12,400)
                  </label>
                  <label className="mt-4 block text-xs text-zinc-500">Action type</label>
                  <select
                    data-testid="proposal-action-type"
                    value={proposal.action_type}
                    onChange={(e) =>
                      setProposal({
                        ...proposal,
                        action_type: e.target.value as AgentProposal["action_type"],
                      })
                    }
                    className="mt-1 w-full rounded-lg border border-white/10 bg-zinc-950 px-3 py-2 text-sm text-white outline-none"
                  >
                    <option value="approve_payment">approve_payment</option>
                    <option value="reject">reject</option>
                  </select>
                </div>
              )}
            </div>
          </details>
        </div>

        {loadError && (
          <p data-testid="invoice-load-error" className="mt-3 text-sm text-red-400">
            {loadError}
          </p>
        )}

        {!hasEvidence && !loadError && (
          <p data-testid="evidence-empty" className="mt-4 text-sm text-zinc-600">
            Upload or paste an invoice, or enter a fetch identifier to begin.
          </p>
        )}
      </section>

      {hasEvidence && proposal && (
        <section className="mt-10">
          <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">
            {usesManualPath ? "2 · Manual scenario" : "2 · Upstream agent (automatic)"}
          </h2>
          <div className="mt-4 grid gap-4 lg:grid-cols-2">
            <div className="rounded-xl border border-white/10 bg-zinc-900/40 p-4 text-sm">
              <p className="font-medium text-white">Evidence summary</p>
              {parsed && (
                <dl className="mt-3 space-y-1 text-zinc-400">
                  <div className="flex justify-between gap-4">
                    <dt>Invoice</dt>
                    <dd className="font-mono text-zinc-200">{parsed.invoice_number}</dd>
                  </div>
                  <div className="flex justify-between gap-4">
                    <dt>Vendor</dt>
                    <dd className="text-zinc-200">{parsed.vendor}</dd>
                  </div>
                  <div className="flex justify-between gap-4">
                    <dt>Total</dt>
                    <dd className="font-mono text-zinc-200">
                      {parsed.total.value} {parsed.total.currency}
                    </dd>
                  </div>
                  <div className="flex justify-between gap-4">
                    <dt>Line items</dt>
                    <dd className="text-zinc-200">{parsed.line_items.length}</dd>
                  </div>
                  <p className="mt-3 text-xs text-zinc-500">
                    Parsed from your uploaded or pasted invoice text — original text is sent to the
                    gate for grounding.
                  </p>
                </dl>
              )}
              {fetchId && (
                <dl className="mt-3 space-y-1 text-zinc-400">
                  <div className="flex justify-between gap-4">
                    <dt>Fetch</dt>
                    <dd className="font-mono text-zinc-200">{fetchId}</dd>
                  </div>
                  <p className="mt-3 text-xs text-zinc-500">
                    Invoice body comes from your system of record at verification time — not from
                    this browser.
                  </p>
                </dl>
              )}
            </div>

            <div className="rounded-xl border border-white/10 bg-zinc-900/40 p-4">
              {!usesManualPath && parsed && (
                <p className="text-sm text-zinc-400">
                  The upstream agent will parse your invoice, propose{" "}
                  <span className="font-mono text-zinc-200">
                    approve_payment {parsed.total.value} {parsed.total.currency}
                  </span>{" "}
                  to <span className="text-zinc-200">{parsed.vendor}</span>, then call{" "}
                  <span className="font-mono text-violet-300">verify_action</span>.
                </p>
              )}
              {parsed && !usesManualPath && (
                <input
                  type="hidden"
                  data-testid="proposal-amount"
                  value={proposal.amount_value}
                  readOnly
                />
              )}
              {(usesManualPath || fetchId) && (
                <>
                  <label className="block text-xs text-zinc-500">Payment amount (USD)</label>
                  <input
                    data-testid="proposal-amount"
                    value={proposal.amount_value}
                    placeholder={fetchId ? "e.g. 3610.00 — required in fetch mode" : undefined}
                    onChange={(e) =>
                      setProposal({
                        ...proposal,
                        amount_value: e.target.value,
                        agent_rationale: "Manual edit.",
                      })
                    }
                    className="mt-1 w-full rounded-lg border border-white/10 bg-zinc-950 px-3 py-2 font-mono text-sm text-white outline-none focus:border-violet-500/50"
                  />
                  {amountMissing && (
                    <p data-testid="amount-required" className="mt-2 text-xs text-amber-400/90">
                      Payment amount is required.
                    </p>
                  )}

                  {fetchId && (
                    <>
                      <label className="mt-4 block text-xs text-zinc-500">Vendor (proposed)</label>
                      <input
                        data-testid="proposal-vendor"
                        value={proposal.vendor}
                        onChange={(e) =>
                          setProposal({
                            ...proposal,
                            vendor: e.target.value,
                            agent_rationale: "Manual edit.",
                          })
                        }
                        className="mt-1 w-full rounded-lg border border-white/10 bg-zinc-950 px-3 py-2 text-sm text-white outline-none focus:border-violet-500/50"
                      />
                    </>
                  )}
                </>
              )}
            </div>
          </div>

          <div className="mt-6 flex flex-wrap items-center gap-3">
            <button
              type="button"
              data-testid="verify"
              disabled={loading || !(canRunAgent || canRunManual)}
              onClick={() => verify()}
              className="rounded-lg bg-violet-600 px-5 py-2 text-sm font-medium text-white hover:bg-violet-500 disabled:opacity-50"
            >
              {loading
                ? "Running…"
                : usesManualPath
                  ? `Run verification (attempt ${attempt})`
                  : "Verify & execute (test payment)"}
            </button>
            {execution?.status === "pending_human_approval" && (
              <>
                <button
                  type="button"
                  data-testid="approve-escalation"
                  disabled={loading}
                  onClick={() => void decideApproval(true)}
                  className="rounded-lg border border-emerald-500/40 px-5 py-2 text-sm text-emerald-300 hover:bg-emerald-500/10"
                >
                  Human approve & pay
                </button>
                <button
                  type="button"
                  data-testid="reject-escalation"
                  disabled={loading}
                  onClick={() => void decideApproval(false)}
                  className="rounded-lg border border-red-500/40 px-5 py-2 text-sm text-red-300 hover:bg-red-500/10"
                >
                  Reject
                </button>
              </>
            )}
            {canFix && (
              <button
                type="button"
                data-testid="apply-fix"
                onClick={applyFixAndResubmit}
                className="rounded-lg border border-emerald-500/40 px-5 py-2 text-sm text-emerald-300 hover:bg-emerald-500/10"
              >
                Apply gate fix & resubmit
              </button>
            )}
          </div>
        </section>
      )}

      <section className="mt-10">
        <div className="rounded-2xl border border-white/10 bg-zinc-900/30 p-5">
          <h2 className="text-sm font-medium uppercase tracking-wide text-zinc-500">Decision</h2>

          {error && (
            <div data-testid="error-panel" className="mt-4 rounded-xl border border-white/10 bg-zinc-950 p-4 text-sm text-zinc-300">
              {error}
            </div>
          )}

          {!decision && !error && (
            <p className="mt-4 text-sm text-zinc-600">Run verification to see the live gate response.</p>
          )}

          {decision && (
            <div className="mt-4 space-y-4">
              <div className="flex flex-wrap items-center gap-4">
                <span
                  data-testid="decision-banner"
                  className={`rounded-lg border px-4 py-2 font-mono text-lg font-semibold uppercase ${BANNER_STYLES[decision.decision]}`}
                >
                  {decision.decision}
                </span>
                <div className="text-sm text-zinc-400">
                  Score{" "}
                  <span data-testid="score" className="font-mono text-zinc-100">
                    {decision.score === null ? "not computed" : decision.score}
                  </span>
                </div>
              </div>

              <div
                data-testid="payment-execution-panel"
                className="rounded-xl border border-white/10 bg-zinc-950/60 p-4"
              >
                <h3 className="text-xs font-medium uppercase tracking-wide text-zinc-500">
                  Payment execution (test rail)
                </h3>
                {!execution ? (
                  <p className="mt-2 text-sm text-zinc-500">
                    No payment ran — use{" "}
                    <span className="font-mono text-zinc-400">Verify &amp; execute (test payment)</span>{" "}
                    on a pasted invoice (not developer manual scenarios). There is no Stripe/Razorpay
                    checkout UI in v1; the orchestrator calls a backend test provider on ALLOW.
                  </p>
                ) : (
                  (() => {
                    const payment = executionPayment(execution);
                    return (
                      <div className="mt-3 space-y-2 text-sm">
                        <p className="text-zinc-300">
                          Status:{" "}
                          <span data-testid="execution-status" className="font-mono text-white">
                            {String(execution.status)}
                          </span>
                        </p>
                        {payment ? (
                          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs text-zinc-500">
                            <dt>Rail</dt>
                            <dd className="font-mono text-zinc-300">
                              {payment.mode} (no external gateway)
                            </dd>
                            <dt>Payment ID</dt>
                            <dd className="font-mono text-emerald-300">{payment.payment_id}</dd>
                            <dt>Amount</dt>
                            <dd className="font-mono text-zinc-300">
                              {payment.amount_value} {payment.amount_currency}
                            </dd>
                            <dt>Vendor</dt>
                            <dd className="font-mono text-zinc-300">{payment.vendor}</dd>
                          </dl>
                        ) : execution.status === "pending_human_approval" ? (
                          <div className="space-y-3">
                            <p className="text-xs text-amber-300/90">
                              Gate escalated — a human must approve before the test payment rail runs.
                              The gate is not re-run; your approval executes the stored proposal.
                            </p>
                            <div className="flex flex-wrap gap-2">
                              <button
                                type="button"
                                data-testid="approve-escalation-panel"
                                disabled={loading}
                                onClick={() => void decideApproval(true)}
                                className="rounded-lg border border-emerald-500/40 px-4 py-2 text-sm text-emerald-300 hover:bg-emerald-500/10 disabled:opacity-50"
                              >
                                Human approve &amp; pay
                              </button>
                              <button
                                type="button"
                                data-testid="reject-escalation-panel"
                                disabled={loading}
                                onClick={() => void decideApproval(false)}
                                className="rounded-lg border border-red-500/40 px-4 py-2 text-sm text-red-300 hover:bg-red-500/10 disabled:opacity-50"
                              >
                                Reject
                              </button>
                            </div>
                          </div>
                        ) : execution.status === "payment_aborted_duplicate" ? (
                          <p className="text-xs text-amber-300/90">
                            Human approval received, but this invoice was already paid — duplicate
                            protection blocked a second payment.
                          </p>
                        ) : execution.status === "rejected_by_human" ? (
                          <p className="text-xs text-zinc-500">Payment rejected by human reviewer.</p>
                        ) : (
                          <p className="text-xs text-zinc-500">
                            Payment was not executed for this outcome (blocked, duplicate, or
                            rejected).
                          </p>
                        )}
                      </div>
                    );
                  })()
                )}
              </div>

              {decision.reasons.length > 0 && (
                <ul data-testid="reasons" className="space-y-2">
                  {decision.reasons.map((r, i) => (
                    <ReasonCard key={`${r.check}-${i}`} reason={r} />
                  ))}
                </ul>
              )}

              {decision.checks.length > 0 && (
                <div className="overflow-x-auto rounded-xl border border-white/10">
                  <table data-testid="checks-table" className="w-full border-collapse text-left text-xs">
                    <thead>
                      <tr className="border-b border-white/10 bg-zinc-950/80 text-zinc-500">
                        <th className="px-3 py-2 font-medium">Check</th>
                        <th className="px-3 py-2 font-medium">Type</th>
                        <th className="px-3 py-2 font-medium">Result</th>
                        <th className="px-3 py-2 font-medium">Detail</th>
                      </tr>
                    </thead>
                    <tbody>
                      {decision.checks.map((c) => (
                        <tr key={c.name} className="border-b border-white/5 align-top">
                          <td className="px-3 py-2 font-mono text-zinc-200">{c.name}</td>
                          <td className="px-3 py-2 text-zinc-500">{c.type}</td>
                          <td className={`px-3 py-2 font-mono ${c.passed ? "text-emerald-400" : "text-red-400"}`}>
                            {c.passed ? "pass" : "fail"}
                          </td>
                          <td className="px-3 py-2 font-mono text-zinc-500">{c.detail}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}

              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-2 rounded-xl border border-white/10 bg-zinc-950/50 p-4 text-xs text-zinc-500">
                {auditId && (
                  <>
                    <dt>Audit ID</dt>
                    <dd className="font-mono text-zinc-300">{auditId}</dd>
                  </>
                )}
                <dt>Evidence used</dt>
                <dd className="font-mono text-zinc-300">
                  {decision.evidence_used.join(", ") || "none"}
                </dd>
                <dt>Trace ID</dt>
                <dd className="font-mono text-zinc-300">
                  {traceUrl ? (
                    <a href={traceUrl} target="_blank" rel="noreferrer" data-testid="trace-id" className="underline">
                      {decision.trace_id}
                    </a>
                  ) : (
                    <span data-testid="trace-id">{decision.trace_id ?? "none"}</span>
                  )}
                </dd>
                <dt>Latency</dt>
                <dd className="font-mono text-zinc-300">
                  {decision.latency_ms !== null ? `${decision.latency_ms} ms` : "n/a"}
                </dd>
              </dl>
            </div>
          )}
        </div>
      </section>

      <p className="mt-8 text-xs text-zinc-600">
        Connected API: <span data-testid="api-base" className="font-mono text-zinc-500">{API_BASE}</span>
      </p>
    </div>
  );
}

export function VerifyDashboard() {
  return (
    <Suspense fallback={null}>
      <VerifyDashboardInner />
    </Suspense>
  );
}
