# LifeOS MCP Server

Exposes LifeOS over the Model Context Protocol so MCP-capable agents
(OpenClaw, Claude Desktop, anything that speaks streamable-HTTP MCP)
can drive it as a set of tools.

The MCP server is a thin adapter container that runs alongside the
main API stack and translates MCP tool calls into HTTP requests against
the LifeOS API. Same agent key authenticates both the inbound MCP
request and the outbound API call, so the access scope is whatever you
grant the key.

---

## One-time setup

### 1. Issue an MCP-scoped agent key

```bash
docker exec -it lifeos-api python scripts/bootstrap_agent_key.py \
    --name OpenClawMCP --domains '*'
```

Use `--domains '*'` (wildcard) so the agent can hit any of the domain
endpoints — health, finance, etc. Lock down later if needed.

The script prints the plaintext key **once**. Copy it.

### 2. Add the key to `.env`

```
MCP_AGENT_KEY=lifeos_agent_<long-random-string>
```

### 3. Bring up the MCP service

```bash
docker compose up -d --build mcp
```

You should now have `lifeos-mcp` in `docker ps`:

```bash
$ docker ps --filter name=lifeos-mcp --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
NAMES         STATUS    PORTS
lifeos-mcp    Up 5s     0.0.0.0:8200->8200/tcp
```

### 4. Verify

From the LAN machine running OpenClaw:

```bash
curl http://<lifeos-host>:8200/health
# → {"status":"ok","lifeos_api":"http://api:8000","auth_configured":true}

# Try an authenticated tool call (MCP discovery).
# IMPORTANT: use /mcp (no trailing slash) — Starlette redirects
# /mcp/ → /mcp and most HTTP clients drop the auth header on
# 307 redirects for security.
curl -X POST http://<lifeos-host>:8200/mcp \
  -H "Authorization: Bearer $MCP_AGENT_KEY" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
```

---

## Pointing OpenClaw at it

OpenClaw needs the streamable-HTTP MCP transport URL and the bearer
token. Configuration looks roughly like:

```yaml
mcp_servers:
  lifeos:
    transport: streamable_http
    url: http://lifeos.local:8200/mcp   # no trailing slash!
    headers:
      Authorization: Bearer ${MCP_AGENT_KEY}
```

(Replace `lifeos.local` with the host's mDNS name or LAN IP. The MCP
service listens on `0.0.0.0:8200`. `/mcp/` with a trailing slash 307s
to `/mcp` and most HTTP clients drop the Authorization header on the
redirect — always use the canonical no-slash form.)

---

## Tools the server exposes

| Tool | Purpose | Calls |
|---|---|---|
| `search_documents` | Hybrid semantic + full-text doc search | `GET /api/search` |
| `ask` | RAG Q&A with source citations | `POST /api/ask` |
| `upload_document` | Send a file (base64 + filename) | `POST /api/documents/upload` |
| `get_document` | Full doc record (AI summary, action items, chunks) | `GET /api/documents/{id}` |
| `list_upcoming_actions` | Action items due in next N days | `GET /api/actions/upcoming` |
| `list_overdue_actions` | Pending action items past due | `GET /api/actions/overdue` |
| `complete_action` | Mark an action done (auto-queues next for recurring) | `PATCH /api/actions/{id}` |
| `list_subjects` | People, pets, vehicles, properties | `GET /api/subjects` |
| `health_summary` | HealthBot bundle (meds, providers, adherence, weight) | `GET /api/agent/health/*` |
| `finance_summary` | FinanceBot summary (debt, obligations, utilization) | `GET /api/agent/finance/summary` |
| `log_metric` | Record a time-series metric | `POST /api/metrics` |
| `get_trend` | Bucketed average + slope + projection | `GET /api/trends/{sid}/{metric}` |

Tools return JSON; the calling LLM can reason over it directly.

---

## Calendar integration (no new code needed)

You said your calendar bot reads Google Calendar. LifeOS Phase 10
already pushes every action item with a `due_date` into your Google
Calendar (one event per item, recurring items get RRULE). So the
flow is:

1. LifeOS extracts an action from a forwarded doc (or you create one
   via `complete_action` / web UI / a future create_action tool).
2. Phase 10's calendar_sync fires and creates a Google Calendar event.
3. Your calendar bot polls Google Calendar and sees it.

If you want OpenClaw to *reason* about upcoming events before they
hit the calendar (e.g. "should this become a calendar entry?"), call
`list_upcoming_actions` from inside its chat flow — that's the same
list LifeOS will push to GCal.

To enable Phase 10 if you haven't yet:

```bash
# Drop OAuth client JSON at /srv/lifeos/auth/google-oauth-client.json
docker exec -it lifeos-api python scripts/bootstrap_google_calendar.py
# In .env, set GOOGLE_CALENDAR_ENABLED=true
docker compose restart api
```

See `docs/specs/03-PHASES-9-12-APPENDICES.md` Phase 10 for the full setup.

---

## Operational notes

- **Health check:** `GET http://<host>:8200/health` (no auth required).
- **Logs:** `docker logs -f lifeos-mcp`.
- **Restart after key rotation:** Update `MCP_AGENT_KEY` in `.env`,
  then `docker compose restart mcp`. The MCP server reads the env at
  startup.
- **Locked out by accident?** If `MCP_AGENT_KEY` is empty in the env,
  the server rejects every request with 503 and a clear message. Check
  the container env: `docker exec lifeos-mcp env | grep MCP`.

## Security notes

- The MCP port (`8200`) is bound to `0.0.0.0` so other LAN machines
  can reach it. That's intentional. Don't expose it to the public
  internet without an auth proxy or wrapper.
- Cloudflare Tunnel users: route `mcp.lifeos.<your-domain>` through
  the tunnel and use Cloudflare Access for an additional auth layer.
- The agent key is the only credential the MCP server checks. Rotate
  by issuing a new key (`bootstrap_agent_key.py`), updating `.env`,
  and restarting the `mcp` service. Revoke the old key with:
  `UPDATE agent_api_keys SET is_active=false WHERE agent_name='OpenClawMCP-old';`
