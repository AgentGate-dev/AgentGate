import Link from "next/link";
import {
  FEATURES,
  GITHUB_URL,
  HOW_IT_WORKS,
  PLANS,
  SITE,
  STATS,
} from "../lib/site";
import { SiteFooter } from "./SiteFooter";
import { SiteNav } from "./SiteNav";

export function LandingPage() {
  return (
    <>
      <SiteNav active="home" />

      <main data-testid="landing-page">
        <section className="relative overflow-hidden border-b border-white/10">
          <div className="hero-grid absolute inset-0 opacity-40" aria-hidden />
          <div className="relative mx-auto max-w-6xl px-6 pb-20 pt-16 md:pt-24">
            <p className="inline-flex rounded-full border border-violet-500/30 bg-violet-500/10 px-3 py-1 text-xs font-medium text-violet-200">
              Authorization for AI agent spending
            </p>
            <h1 className="mt-6 max-w-4xl text-4xl font-semibold tracking-tight text-white md:text-6xl md:leading-[1.05]">
              Stop payment mistakes before money moves
            </h1>
            <p className="mt-6 max-w-2xl text-lg leading-relaxed text-zinc-400">
              {SITE.description}
            </p>
            <div className="mt-8 flex flex-wrap gap-3">
              <Link
                href="/demo"
                data-testid="hero-demo-cta"
                className="rounded-lg bg-violet-600 px-6 py-3 text-sm font-medium text-white hover:bg-violet-500"
              >
                Try live demo
              </Link>
              <Link
                href={GITHUB_URL}
                target="_blank"
                rel="noreferrer"
                className="rounded-lg border border-white/15 px-6 py-3 text-sm font-medium text-zinc-200 hover:border-white/30"
              >
                Deploy yourself
              </Link>
            </div>

            <dl className="mt-14 grid gap-6 sm:grid-cols-3">
              {STATS.map((stat) => (
                <div
                  key={stat.label}
                  className="rounded-2xl border border-white/10 bg-zinc-900/40 p-5 backdrop-blur"
                >
                  <dt className="text-2xl font-semibold text-white">{stat.value}</dt>
                  <dd className="mt-1 text-sm font-medium text-zinc-300">{stat.label}</dd>
                  <dd className="mt-1 text-xs text-zinc-500">{stat.detail}</dd>
                </div>
              ))}
            </dl>
          </div>
        </section>

        <section id="features" className="mx-auto max-w-6xl px-6 py-20">
          <div className="max-w-2xl">
            <p className="text-sm font-medium uppercase tracking-wider text-violet-400">Product</p>
            <h2 className="mt-2 text-3xl font-semibold text-white">
              Everything you need to gate agent spend
            </h2>
            <p className="mt-3 text-zinc-400">
              AgentGate sits between your agent and payment systems — verifying proposals against
              evidence, enforcing policy, and returning actionable decisions.
            </p>
          </div>
          <div className="mt-12 grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
            {FEATURES.map((feature) => (
              <article
                key={feature.title}
                className="rounded-2xl border border-white/10 bg-zinc-900/30 p-6 transition hover:border-violet-500/30"
              >
                <h3 className="text-lg font-medium text-white">{feature.title}</h3>
                <p className="mt-2 text-sm leading-relaxed text-zinc-400">{feature.description}</p>
              </article>
            ))}
          </div>
        </section>

        <section id="how-it-works" className="border-y border-white/10 bg-zinc-900/20 py-20">
          <div className="mx-auto max-w-6xl px-6">
            <div className="max-w-2xl">
              <p className="text-sm font-medium uppercase tracking-wider text-violet-400">
                How it works
              </p>
              <h2 className="mt-2 text-3xl font-semibold text-white">
                From proposal to decision in milliseconds
              </h2>
            </div>
            <ol className="mt-12 grid gap-6 md:grid-cols-3">
              {HOW_IT_WORKS.map((item) => (
                <li
                  key={item.step}
                  className="rounded-2xl border border-white/10 bg-zinc-950/60 p-6"
                >
                  <span className="text-sm font-mono text-violet-400">{item.step}</span>
                  <h3 className="mt-3 text-lg font-medium text-white">{item.title}</h3>
                  <p className="mt-2 text-sm leading-relaxed text-zinc-400">{item.description}</p>
                </li>
              ))}
            </ol>

            <div className="mt-12 rounded-2xl border border-white/10 bg-zinc-950/80 p-6 md:p-8">
              <h3 className="text-lg font-medium text-white">Try it on your invoices</h3>
              <p className="mt-2 max-w-2xl text-sm text-zinc-400">
                Upload a real invoice file or paste extracted text — no preset scenarios or
                hand-built JSON. The gate verifies against your document or your system of record.
              </p>
              <Link
                href="/demo"
                data-testid="home-try-demo"
                className="mt-6 inline-flex rounded-lg bg-violet-600 px-5 py-2.5 text-sm font-medium text-white hover:bg-violet-500"
              >
                Open live verification
              </Link>
            </div>
          </div>
        </section>

        <section id="pricing" className="mx-auto max-w-6xl px-6 py-20">
          <div className="max-w-2xl">
            <p className="text-sm font-medium uppercase tracking-wider text-violet-400">Pricing</p>
            <h2 className="mt-2 text-3xl font-semibold text-white">
              Start free. Scale on your terms.
            </h2>
            <p className="mt-3 text-zinc-400">
              Evaluate on our sandbox, self-host the open-source core, or talk to us for enterprise
              deployment and support.
            </p>
          </div>
          <div className="mt-12 grid gap-6 lg:grid-cols-3">
            {PLANS.map((plan) => (
              <article
                key={plan.id}
                data-testid={`plan-${plan.id}`}
                className={`flex flex-col rounded-2xl border p-6 ${
                  plan.highlighted
                    ? "border-violet-500/50 bg-violet-500/10 shadow-lg shadow-violet-950/30"
                    : "border-white/10 bg-zinc-900/30"
                }`}
              >
                <h3 className="text-lg font-medium text-white">{plan.name}</h3>
                <p className="mt-3 text-3xl font-semibold text-white">{plan.price}</p>
                <p className="text-xs uppercase tracking-wide text-zinc-500">{plan.period}</p>
                <p className="mt-4 text-sm leading-relaxed text-zinc-400">{plan.description}</p>
                <ul className="mt-6 flex-1 space-y-2 text-sm text-zinc-300">
                  {plan.features.map((feature) => (
                    <li key={feature} className="flex gap-2">
                      <span className="text-violet-400">✓</span>
                      <span>{feature}</span>
                    </li>
                  ))}
                </ul>
                <Link
                  href={plan.href}
                  target={plan.href.startsWith("http") || plan.href.startsWith("mailto") ? "_blank" : undefined}
                  rel={
                    plan.href.startsWith("http") || plan.href.startsWith("mailto")
                      ? "noreferrer"
                      : undefined
                  }
                  data-testid={`plan-cta-${plan.id}`}
                  className={`mt-8 block rounded-lg px-4 py-2.5 text-center text-sm font-medium ${
                    plan.highlighted
                      ? "bg-violet-600 text-white hover:bg-violet-500"
                      : "border border-white/15 text-zinc-200 hover:border-white/30"
                  }`}
                >
                  {plan.cta}
                </Link>
              </article>
            ))}
          </div>
        </section>

        <section className="border-t border-white/10 bg-violet-950/20 py-16">
          <div className="mx-auto flex max-w-6xl flex-col items-start justify-between gap-6 px-6 md:flex-row md:items-center">
            <div>
              <h2 className="text-2xl font-semibold text-white">Ready to protect agent spend?</h2>
              <p className="mt-2 max-w-xl text-sm text-zinc-400">
                Launch the live demo in one click, or install AgentGate in your environment today.
              </p>
            </div>
            <div className="flex flex-wrap gap-3">
              <Link
                href="/demo"
                className="rounded-lg bg-white px-5 py-2.5 text-sm font-medium text-zinc-950 hover:bg-zinc-100"
              >
                Open live demo
              </Link>
              <Link
                href={GITHUB_URL}
                target="_blank"
                rel="noreferrer"
                className="rounded-lg border border-white/20 px-5 py-2.5 text-sm font-medium text-white hover:border-white/40"
              >
                View GitHub
              </Link>
            </div>
          </div>
        </section>
      </main>

      <SiteFooter />
    </>
  );
}
