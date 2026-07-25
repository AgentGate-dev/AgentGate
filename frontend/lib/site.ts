export const GITHUB_URL = "https://github.com/varunk14/AgentGate";
export const DOCS_QUICKSTART = `${GITHUB_URL}#quickstart`;

export const SITE = {
  name: "AgentGate",
  tagline: "The authorization layer for AI agent spending",
  description:
    "Verify every agent-proposed payment against invoice evidence before it executes. Deterministic allow, block, or escalate — with a machine-readable reason your agent can act on.",
} as const;

export const STATS = [
  { value: "3", label: "Decision outcomes", detail: "Allow · Block · Escalate" },
  { value: "<50ms", label: "Verification latency", detail: "No LLM in the gate path" },
  { value: "100%", label: "Deterministic core", detail: "Decimal arithmetic only" },
] as const;

export const FEATURES = [
  {
    title: "Evidence-backed verification",
    description:
      "Every proposed payment is checked against invoice source text (required in caller mode) or fetched from your system of record — never agent-forged JSON alone.",
  },
  {
    title: "Machine-readable blocks",
    description:
      "Blocked actions return the exact field to fix and the expected value — like a compiler error, so agents can self-correct and resubmit.",
  },
  {
    title: "Policy guardrails",
    description:
      "Amount ceilings, grounding coverage floors, and escalation rules ship in YAML. Policy adds escalations; it never opens the gate.",
  },
  {
    title: "Fail closed by design",
    description:
      "Malformed input, missing evidence, or internal errors become escalate — never crash, never silently allow.",
  },
  {
    title: "HTTP API and MCP",
    description:
      "Integrate via REST for orchestrators or stdio MCP for agent runtimes. Same envelope, same decision contract everywhere.",
  },
  {
    title: "Human-in-the-loop",
    description:
      "When policy or ambiguity requires a person, escalate with full trace context. Approvals route execution without rewriting the audit record.",
  },
] as const;

export const HOW_IT_WORKS = [
  {
    step: "01",
    title: "Agent proposes an action",
    description:
      "Your agent reads an invoice at payment time and proposes approve_payment with amount, vendor, and evidence — or asks the gate to fetch from your records.",
  },
  {
    step: "02",
    title: "AgentGate verifies deterministically",
    description:
      "Frame checks, arithmetic, currency, grounding, and policy run as pure functions. No model call in the verification path.",
  },
  {
    step: "03",
    title: "Allow, block, or escalate",
    description:
      "Consistent actions proceed. Fixable mistakes block with a reason. Edge cases and policy hits route to a human reviewer.",
  },
] as const;

export const PLANS = [
  {
    id: "sandbox",
    name: "Live sandbox",
    price: "Free",
    period: "always",
    description: "Try the gate against our hosted API. Perfect for evaluation and demos.",
    cta: "Open live demo",
    href: "/demo",
    highlighted: false,
    features: [
      "Full POST /verify API",
      "Bring your own invoices",
      "No account required",
      "Cold start on free tier (~1 min)",
    ],
  },
  {
    id: "self-host",
    name: "Self-hosted",
    price: "Open source",
    period: "MIT license",
    description: "Run AgentGate in your stack. Ship the default policy or bring your own.",
    cta: "View on GitHub",
    href: GITHUB_URL,
    highlighted: true,
    features: [
      "pip install agentgate",
      "HTTP API + MCP server",
      "Fetch mode from your records",
      "Langfuse tracing optional",
    ],
  },
  {
    id: "enterprise",
    name: "Enterprise",
    price: "Contact us",
    period: "custom deployment",
    description: "Dedicated deployment, custom policies, and integration support for production agent spend.",
    cta: "Talk to sales",
    href: "mailto:hello@agentgate.dev?subject=AgentGate%20Enterprise",
    highlighted: false,
    features: [
      "Private deployment",
      "System-of-record connectors",
      "Slack / card-rail integrations",
      "Priority support",
    ],
  },
] as const;

export const FOOTER_LINKS = {
  product: [
    { label: "Live demo", href: "/demo" },
    { label: "How it works", href: "/#how-it-works" },
    { label: "Pricing", href: "/#pricing" },
  ],
  developers: [
    { label: "GitHub", href: GITHUB_URL },
    { label: "Quickstart", href: DOCS_QUICKSTART },
    { label: "Real-world problem", href: `${GITHUB_URL}/blob/main/docs/real-world-problem.md` },
    { label: "API reference", href: `${GITHUB_URL}/blob/main/README.md` },
  ],
} as const;
