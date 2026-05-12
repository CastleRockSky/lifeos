"""
LifeOS MCP server — exposes LifeOS over the Model Context Protocol so that
MCP-capable agents (Claude Desktop, OpenClaw, etc.) can ask questions,
upload documents, list upcoming actions, query metrics, and trigger
domain-bot summaries.

Architecture: this is a thin adapter that translates MCP tool calls into
HTTP requests against the LifeOS API container (http://api:8000 inside
the docker network). The same agent key is used for both the inbound MCP
auth check AND the outbound LifeOS API calls, so the scope of access
matches whatever you grant the OpenClaw key.

Auth: every inbound request must carry `Authorization: Bearer <key>` or
`X-Agent-Key: <key>` matching MCP_AGENT_KEY in the environment.
"""

import base64
import contextlib
import json
import logging
import os
import tempfile
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("lifeos-mcp")

LIFEOS_API_URL = os.environ.get("LIFEOS_API_URL", "http://api:8000")
MCP_AGENT_KEY = os.environ.get("MCP_AGENT_KEY", "")
MCP_PORT = int(os.environ.get("MCP_PORT", "8200"))
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")

if not MCP_AGENT_KEY:
    logger.warning(
        "MCP_AGENT_KEY is empty — the server will reject ALL requests. "
        "Issue a key with scripts/bootstrap_agent_key.py and set it in .env."
    )


# ── HTTP client shared across tool calls ────────────────────────────────

_client: Optional[httpx.AsyncClient] = None


def _api_headers() -> dict:
    """Headers for outbound LifeOS API calls (uses MCP_AGENT_KEY)."""
    return {"X-Agent-Key": MCP_AGENT_KEY} if MCP_AGENT_KEY else {}


async def _client_get() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=LIFEOS_API_URL,
            timeout=httpx.Timeout(60.0, connect=5.0),
            headers=_api_headers(),
        )
    return _client


# ── MCP server + tools ──────────────────────────────────────────────────

mcp = FastMCP(
    name="LifeOS",
    instructions=(
        "LifeOS is Dave's personal life management system. It stores documents "
        "(medical, financial, auto, home, vet, legal, insurance) with AI-extracted "
        "structured records, recurring action items, and time-series metrics. "
        "Use these tools to search docs, ask questions in natural language, "
        "upload new files, surface upcoming/overdue actions, and read domain "
        "summaries. Action items with due_date are mirrored to Google Calendar "
        "automatically — you don't need to create calendar events yourself."
    ),
)


# ── Documents ───────────────────────────────────────────────────────────

@mcp.tool()
async def search_documents(
    query: str,
    domain: Optional[str] = None,
    limit: int = 10,
) -> dict:
    """Hybrid (semantic + full-text) search over LifeOS documents.

    Args:
        query: Free-text search query.
        domain: Optional domain filter (medical, financial, auto, home, vet,
                legal, insurance).
        limit: Max results (1-50).

    Returns a list of matching documents with id, title, domain, category,
    relevance score, and ingestion date.
    """
    client = await _client_get()
    params = {"q": query, "limit": min(max(limit, 1), 50)}
    if domain:
        params["domain"] = domain
    r = await client.get("/api/search", params=params)
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def ask(question: str, domain: Optional[str] = None) -> dict:
    """Ask LifeOS a question. Uses RAG over your documents and returns an
    answer with source citations.

    Args:
        question: Natural-language question.
        domain: Optional domain filter to scope the answer.

    Returns: {"answer": str, "sources": [{document_id, title, domain, relevance}]}
    """
    client = await _client_get()
    body: dict[str, Any] = {"question": question}
    if domain:
        body["domain"] = domain
    r = await client.post("/api/ask", json=body)
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def upload_document(
    filename: str,
    content_base64: str,
    title: Optional[str] = None,
    domain: Optional[str] = None,
    category: Optional[str] = None,
    tags: Optional[str] = None,
) -> dict:
    """Upload a document to LifeOS. Pass the file contents as base64.
    OCR and AI classification run automatically in the background.

    Args:
        filename: Original filename (used for type detection).
        content_base64: Base64-encoded file bytes.
        title: Optional title (defaults to filename; AI may overwrite).
        domain: Optional domain hint (medical, financial, auto, etc.).
        category: Optional category hint.
        tags: Comma-separated tags.

    Returns the created document's id, file_type, size, and initial
    embedding/AI status. Poll get_document(id) to see analysis results.
    """
    try:
        content = base64.b64decode(content_base64)
    except Exception as e:
        raise ValueError(f"content_base64 is not valid base64: {e}")

    if len(content) > 100 * 1024 * 1024:
        raise ValueError("File exceeds 100 MB upload limit")

    client = await _client_get()
    files = {"file": (filename, content)}
    data: dict[str, str] = {}
    if title:
        data["title"] = title
    if domain:
        data["domain"] = domain
    if category:
        data["category"] = category
    if tags:
        data["tags"] = tags
    r = await client.post("/api/documents/upload", files=files, data=data)
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def get_document(document_id: str) -> dict:
    """Fetch a document by id — full record including AI summary, extracted
    data, action items, chunks, and subject linkage.
    """
    client = await _client_get()
    r = await client.get(f"/api/documents/{document_id}")
    r.raise_for_status()
    return r.json()


# ── Actions ─────────────────────────────────────────────────────────────

@mcp.tool()
async def list_upcoming_actions(days: int = 30, domain: Optional[str] = None) -> dict:
    """List action items coming due in the next `days` days, ordered by
    due_date ASC. Includes recurring items, refill reminders, registration
    renewals, vaccinations, payment due dates, etc.

    These are also pushed to Google Calendar automatically if Phase 10 is
    enabled, so your calendar bot will see them there.
    """
    client = await _client_get()
    params: dict[str, Any] = {"days": max(1, min(days, 365))}
    if domain:
        params["domain"] = domain
    r = await client.get("/api/actions/upcoming", params=params)
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def list_overdue_actions(domain: Optional[str] = None) -> dict:
    """List action items whose due_date is in the past and are still
    pending. Often a good starting point for "what should I tackle today."
    """
    client = await _client_get()
    params: dict[str, Any] = {}
    if domain:
        params["domain"] = domain
    r = await client.get("/api/actions/overdue", params=params)
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def complete_action(action_id: str, notes: Optional[str] = None) -> dict:
    """Mark an action item as completed. For recurring items, completing
    one occurrence automatically queues the next.
    """
    client = await _client_get()
    body: dict[str, Any] = {"status": "completed"}
    if notes:
        body["notes"] = notes
    r = await client.patch(f"/api/actions/{action_id}", json=body)
    r.raise_for_status()
    return r.json()


# ── Subjects ────────────────────────────────────────────────────────────

@mcp.tool()
async def list_subjects() -> dict:
    """List all subjects (people, pets, vehicles, properties) on file
    with their type and profile data.
    """
    client = await _client_get()
    r = await client.get("/api/subjects")
    r.raise_for_status()
    return r.json()


# ── Domain bots (already X-Agent-Key authed) ────────────────────────────

@mcp.tool()
async def health_summary(subject: str = "dave") -> dict:
    """HealthBot summary: latest weight/BP/A1C, active medications with
    refill dates, providers, recent labs, medication adherence %.
    """
    client = await _client_get()
    # Bundle several agent/health endpoints into one
    meds = await client.get("/api/agent/health/medications", params={"subject": subject})
    providers = await client.get("/api/agent/health/providers", params={"subject": subject})
    adherence = await client.get(
        "/api/agent/health/medications/adherence",
        params={"subject": subject, "days": 30},
    )
    weight = await client.get(
        "/api/agent/health/metrics",
        params={"subject": subject, "type": "weight", "days": 30},
    )
    return {
        "subject": subject,
        "medications": meds.json().get("data", []),
        "providers": providers.json().get("data", []),
        "adherence_30d": adherence.json().get("data", {}),
        "weight_30d": weight.json().get("data", []),
    }


@mcp.tool()
async def finance_summary(subject: str = "dave") -> dict:
    """FinanceBot summary: total debt, monthly obligations, credit
    utilization, upcoming 7-day payments, coverage gaps.
    """
    client = await _client_get()
    r = await client.get("/api/agent/finance/summary", params={"subject": subject})
    r.raise_for_status()
    return r.json()


# ── Metrics + trends ────────────────────────────────────────────────────

@mcp.tool()
async def log_metric(
    subject_id: str,
    metric_type: str,
    value: Optional[float] = None,
    value_text: Optional[str] = None,
    notes: Optional[str] = None,
) -> dict:
    """Record a time-series metric. Use for ad-hoc tracking outside of
    document extraction — e.g. weight check, BP reading, mileage update,
    pet weight at home.

    Args:
        subject_id: UUID of the subject (use list_subjects to get IDs).
        metric_type: snake_case identifier — e.g. weight, blood_pressure_systolic,
                     blood_pressure_diastolic, pet_weight, mileage, net_worth.
        value: Numeric value (use either this or value_text).
        value_text: Non-numeric value like "120/80" — use either this or value.
        notes: Free-text note.
    """
    client = await _client_get()
    body: dict[str, Any] = {"subject_id": subject_id, "metric_type": metric_type}
    if value is not None:
        body["value_numeric"] = value
    if value_text:
        body["value_text"] = value_text
    if notes:
        body["notes"] = notes
    r = await client.post("/api/metrics", json=body)
    r.raise_for_status()
    return r.json()


@mcp.tool()
async def get_trend(
    subject_id: str,
    metric_type: str,
    period: str = "weekly",
    range: str = "90d",
    goal: Optional[float] = None,
) -> dict:
    """Aggregated trend for a metric — bucketed averages, slope, direction,
    and optional projected goal date.

    Args:
        subject_id: UUID of the subject.
        metric_type: e.g. weight, a1c, credit_card_balance, mileage.
        period: daily | weekly | monthly.
        range: 30d | 90d | 6m | 1y | all.
        goal: Optional target value to compute projected_goal_date.
    """
    client = await _client_get()
    params: dict[str, Any] = {"period": period, "range": range}
    if goal is not None:
        params["goal"] = goal
    r = await client.get(f"/api/trends/{subject_id}/{metric_type}", params=params)
    r.raise_for_status()
    return r.json()


# ── Custom auth middleware (pure ASGI) ──────────────────────────────────
#
# We can't use Starlette's BaseHTTPMiddleware here because it buffers the
# entire response body to let dispatch() inspect/modify it — that breaks
# streaming responses (and streamable-HTTP MCP is streaming end-to-end).
# Pure ASGI middleware passes events through without buffering.

class AuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # Health probe is open so docker healthchecks + operators can diagnose
        # the auth-config state without needing the key.
        if path == "/health":
            await self.app(scope, receive, send)
            return

        if not MCP_AGENT_KEY:
            await _send_json_response(send, 503, {
                "error": "MCP_AGENT_KEY not configured on the server",
            })
            return

        # Extract Bearer token or X-Agent-Key (case-insensitive header lookup)
        token: Optional[str] = None
        for raw_name, raw_value in scope.get("headers", []):
            name = raw_name.decode("latin-1").lower()
            if name == "authorization":
                v = raw_value.decode("latin-1")
                if v.lower().startswith("bearer "):
                    token = v[7:].strip()
                    break
            elif name == "x-agent-key" and not token:
                token = raw_value.decode("latin-1").strip()

        if token != MCP_AGENT_KEY:
            await _send_json_response(send, 401, {
                "error": "invalid_token",
                "message": "Provide MCP_AGENT_KEY via Authorization: Bearer <key> or X-Agent-Key header",
            })
            return

        await self.app(scope, receive, send)


async def _send_json_response(send, status: int, body: dict):
    payload = json.dumps(body).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(payload)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": payload, "more_body": False})


# ── Entry point ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    from starlette.applications import Starlette
    from starlette.routing import Mount, Route
    import uvicorn

    # FastMCP exposes its underlying Starlette app via .streamable_http_app()
    # (the modern transport). Don't use BaseHTTPMiddleware to wrap it — that
    # buffers responses and breaks streaming.
    inner = mcp.streamable_http_app()

    async def health(_request):
        return JSONResponse({
            "status": "ok",
            "lifeos_api": LIFEOS_API_URL,
            "auth_configured": bool(MCP_AGENT_KEY),
        })

    # FastMCP's app has its own lifespan that initializes the streamable-HTTP
    # session manager's task group. Starlette does NOT run a mounted sub-app's
    # lifespan automatically — so without this, the first request hits an
    # uninitialized session manager and 500s with "Task group is not
    # initialized." Re-enter the inner lifespan from our outer lifespan.
    @contextlib.asynccontextmanager
    async def lifespan(_app):
        async with inner.router.lifespan_context(inner):
            yield

    base = Starlette(
        debug=False,
        routes=[
            Route("/health", health),
            Mount("/", app=inner),
        ],
        lifespan=lifespan,
    )
    app = AuthMiddleware(base)

    logger.info(f"LifeOS MCP server starting on {MCP_HOST}:{MCP_PORT}")
    logger.info(f"  LifeOS API: {LIFEOS_API_URL}")
    logger.info(f"  Auth: {'enabled' if MCP_AGENT_KEY else 'DISABLED (rejecting all requests)'}")
    uvicorn.run(app, host=MCP_HOST, port=MCP_PORT, log_level="info")
