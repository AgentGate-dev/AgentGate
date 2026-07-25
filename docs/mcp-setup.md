# AgentGate MCP setup

AgentGate splits into two MCP servers so the **core gate stays tiny** and upstream
work scales independently.

## Architecture

```text
Invoice PDF/email
       ↓
  agentgate-upstream-mcp     parse_invoice · propose_payment · process_invoice
       ↓
  agentgate-mcp              verify_action  →  allow | block | escalate
       ↓
  Your orchestrator          pay only on allow
```

| Server | Tools | Role |
|--------|-------|------|
| **`agentgate-mcp`** | `verify_action` | Core gate — ALLOW / BLOCK / ESCALATE only |
| **`agentgate-upstream-mcp`** | `parse_invoice`, `propose_payment`, `process_invoice`, `process_fetch_payment` | Upstream agent work before the gate |

## Install

```bash
cd backend
pip install -e ".[server,mcp]"
```

## Cursor / Claude Desktop config

Add to your MCP settings (adjust paths):

```json
{
  "mcpServers": {
    "agentgate": {
      "command": "/path/to/AgentGate/.venv/bin/agentgate-mcp",
      "env": {
        "AGENTGATE_RECORDS_DIR": "/path/to/AgentGate/backend/data/system_of_record"
      }
    },
    "agentgate-upstream": {
      "command": "/path/to/AgentGate/.venv/bin/agentgate-upstream-mcp"
    }
  }
}
```

## Typical agent flow (scalable)

1. Agent reads invoice (your PDF tool / email / ERP).
2. Call **`parse_invoice`** with the extracted text.
3. Call **`propose_payment`** (or your LLM) to build `proposed_action`.
4. Call **`verify_action`** on **`agentgate-mcp`** with `proposed_action` + `source`.
5. On **block**, fix the field named in `field_to_change` and call **`verify_action`** again.
6. On **allow**, your orchestrator executes payment.

One-shot demo: **`process_invoice`** on upstream MCP (parse + propose + verify in one call).

## HTTP demo (no manual JSON)

```bash
curl -s -X POST http://127.0.0.1:8000/agent/process \
  -H 'Content-Type: application/json' \
  -d '{"raw_text": "...(paste invoice text)..."}'
```

Returns `parsed_invoice`, `proposed_action`, and `decision`.

## Environment

Same as the HTTP API:

- `AGENTGATE_RECORDS_DIR` — fetch mode records (optional)
- `AGENTGATE_CORS_ORIGINS` — for the web demo only
