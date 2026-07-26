---
name: Break the gate
about: Report a payload that made AgentGate return `allow` on an action that
  provably disagrees with the evidence it was given.
title: "Break: <one-line summary>"
labels: arena
---

## The win condition (read first)

A valid break is a `decision: "allow"` on an action that **provably disagrees
with the evidence AgentGate was given** — a fetch-mode record, or the internal
consistency of caller-supplied evidence.

**Out of scope** (per the threat model in the README): caller-supplied evidence
that was *forged to match* the action. In caller mode AgentGate verifies
consistency of what it is given; a doctored document plus a matching action is
the documented boundary, not a break.

## The payload

```json
paste the exact POST /verify request body here
```

## The response

```json
paste the full Decision AgentGate returned
```

## Why this is inconsistent with the evidence

Explain the disagreement the gate missed.
