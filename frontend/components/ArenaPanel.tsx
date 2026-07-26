"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { GITHUB_URL } from "../lib/site";

// The published truth (D56): a scheduled public CI job runs the in-repo attack
// suite against the live gate every 6 hours and publishes this JSON to
// gh-pages. This page is a dumb pipe (D39): it renders exactly what the JSON
// says; a fetch failure renders an error panel, never a synthesized counter.
const RESULTS_URL =
  process.env.NEXT_PUBLIC_ARENA_RESULTS_URL ??
  "https://raw.githubusercontent.com/varunk14/AgentGate/gh-pages/arena/results.json";

const SUITE_URL = `${GITHUB_URL}/blob/main/backend/arena/suite.jsonl`;
const WORKFLOW_URL = `${GITHUB_URL}/actions/workflows/arena.yml`;
const REPORT_URL = `${GITHUB_URL}/issues/new?template=break-the-gate.md`;

interface ArenaResults {
  run_at: string;
  total: number;
  counts: {
    allow: number;
    block: number;
    escalate: number;
    transport_error: number;
  };
  false_allows: { id: string }[];
  zero_false_allows_since: string | null;
}

function Stat({ label, value, testId }: { label: string; value: string; testId?: string }) {
  return (
    <div className="rounded-xl border border-white/10 bg-zinc-900/40 p-4">
      <p className="text-xs uppercase tracking-wide text-zinc-500">{label}</p>
      <p data-testid={testId} className="mt-1 font-mono text-2xl text-white">
        {value}
      </p>
    </div>
  );
}

export function ArenaPanel() {
  const [results, setResults] = useState<ArenaResults | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetch(RESULTS_URL, { cache: "no-store" })
      .then(async (resp) => {
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        return (await resp.json()) as ArenaResults;
      })
      .then((data) => {
        if (!cancelled) setResults(data);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(
            `No published arena results could be loaded (${String(err)}). ` +
              "The first scheduled run may not have happened yet — the attack suite and its schedule are public either way.",
          );
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div data-testid="arena-panel">
      <header className="border-b border-white/10 pb-8">
        <p className="text-sm font-medium uppercase tracking-wider text-violet-400">
          Public adversarial arena
        </p>
        <h1 className="mt-2 text-3xl font-semibold tracking-tight text-white">
          The attack suite is public. So are the results.
        </h1>
        <p className="mt-3 max-w-3xl text-sm leading-relaxed text-zinc-400">
          Every six hours a public CI job replays{" "}
          <a href={SUITE_URL} className="text-violet-300 underline" target="_blank" rel="noreferrer">
            an in-repo suite of adversarial payloads
          </a>{" "}
          — tampered totals, vendor swaps, frame attacks, malformed JSON, decimal traps —
          against the live gate, and{" "}
          <a href={WORKFLOW_URL} className="text-violet-300 underline" target="_blank" rel="noreferrer">
            the job goes red
          </a>{" "}
          if a single one is allowed. &quot;Fail-closed&quot; as a continuously attacked,
          independently checkable fact — not a README claim.
        </p>
      </header>

      <section className="mt-8">
        {error && (
          <div
            data-testid="arena-error"
            className="rounded-xl border border-white/10 bg-zinc-950 p-4 text-sm text-zinc-300"
          >
            {error}
          </div>
        )}

        {results && (
          <>
            <div
              data-testid="arena-status"
              className={`rounded-xl border px-4 py-3 font-mono text-sm ${
                results.false_allows.length === 0
                  ? "border-emerald-500/40 bg-emerald-500/10 text-emerald-300"
                  : "border-red-500/40 bg-red-500/10 text-red-300"
              }`}
            >
              {results.false_allows.length === 0
                ? `false allows: 0${
                    results.zero_false_allows_since
                      ? ` since ${results.zero_false_allows_since.slice(0, 10)}`
                      : ""
                  }`
                : `FALSE ALLOWS: ${results.false_allows.length} — the gate was beaten; see the run log`}
            </div>

            <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              <Stat label="Payloads per run" value={String(results.total)} testId="arena-total" />
              <Stat label="Blocked" value={String(results.counts.block)} testId="arena-blocked" />
              <Stat label="Escalated" value={String(results.counts.escalate)} testId="arena-escalated" />
              <Stat label="Allowed (controls)" value={String(results.counts.allow)} testId="arena-allowed" />
            </div>

            <p className="mt-3 text-xs text-zinc-500">
              Last run: <span className="font-mono">{results.run_at}</span>
              {results.counts.transport_error > 0 && (
                <span className="ml-2 text-amber-400">
                  ({results.counts.transport_error} transport errors this run)
                </span>
              )}
            </p>
          </>
        )}
      </section>

      <section className="mt-10 grid gap-4 lg:grid-cols-2">
        <div className="rounded-xl border border-white/10 bg-zinc-900/40 p-5 text-sm">
          <h2 className="font-medium text-white">Bring your own attack</h2>
          <p className="mt-2 text-zinc-400">
            The suite is the floor, not the ceiling.{" "}
            <Link href="/demo" className="text-violet-300 underline">
              Try to break it live in the demo
            </Link>{" "}
            (paste anything into the gate), or{" "}
            <a href={REPORT_URL} className="text-violet-300 underline" target="_blank" rel="noreferrer">
              report a break
            </a>
            . Bounty at launch: get named in the README.
          </p>
        </div>
        <div className="rounded-xl border border-white/10 bg-zinc-900/40 p-5 text-sm">
          <h2 className="font-medium text-white">What counts as a break</h2>
          <p className="mt-2 text-zinc-400">
            A <span className="font-mono text-zinc-300">decision: &quot;allow&quot;</span> on an
            action that provably disagrees with the evidence AgentGate was given. Out of
            scope, per the threat model: caller-supplied evidence forged to match the
            action — in caller mode the gate verifies consistency of what it is given;
            defeating forged evidence is what fetch mode and enforced execution are for.
          </p>
        </div>
      </section>
    </div>
  );
}
