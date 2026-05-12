"""
agent_auth.py — X-Agent-Key authentication for the agent-facing API (Phase 5).

Agent keys are issued out-of-band (via scripts/bootstrap_agent_key.py) and
stored as SHA-256 hashes in agent_api_keys. Each key carries an allow-list of
domains; require_agent_domain() enforces scope at the route level.

Usage:
    @router.get("/something")
    async def endpoint(_: None = Depends(require_agent_domain("medical"))):
        ...
"""

import hashlib
import logging
from datetime import datetime, timezone

from fastapi import Header, HTTPException, status

from database import get_pool

logger = logging.getLogger(__name__)


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


async def validate_agent_key(plaintext: str) -> dict:
    """Look up an agent key, return its row, raise 401 if invalid/inactive."""
    if not plaintext:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"error": "missing_key", "message": "X-Agent-Key header required"},
        )

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT id, agent_name, allowed_domains, is_active
            FROM agent_api_keys
            WHERE key_hash = $1 AND deleted_at IS NULL
        """, hash_key(plaintext))

    if not row or not row["is_active"]:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            detail={"error": "invalid_key", "message": "Agent key is invalid or inactive"},
        )

    # Best-effort last_used update (don't block on failure)
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE agent_api_keys SET last_used_at = $1 WHERE id = $2",
                datetime.now(timezone.utc), row["id"],
            )
    except Exception as e:
        logger.warning(f"agent key last_used update failed: {e}")

    return {
        "id": str(row["id"]),
        "agent_name": row["agent_name"],
        "allowed_domains": list(row["allowed_domains"] or []),
    }


def require_agent_domain(domain: str):
    """Return a FastAPI dependency that enforces an agent key scoped to `domain`."""
    async def dependency(x_agent_key: str = Header(default=None, alias="X-Agent-Key")) -> dict:
        info = await validate_agent_key(x_agent_key)
        if domain not in info["allowed_domains"] and "*" not in info["allowed_domains"]:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "domain_forbidden",
                    "message": f"Agent '{info['agent_name']}' not authorized for domain '{domain}'",
                },
            )
        return info
    return dependency
