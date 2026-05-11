"""
helpers.py — Shared utilities for routes.
"""

import uuid
import logging

from fastapi import Request

from database import get_pool

logger = logging.getLogger(__name__)


def get_user_email(request: Request) -> str:
    return request.headers.get("cf-access-authenticated-user-email", "local")


async def audit_log(
    action: str,
    user_email: str = "local",
    table_name: str = None,
    record_id: str = None,
    details: dict = None,
):
    pool = get_pool()
    if not pool:
        return
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO audit_log (action, user_email, table_name, record_id, details)
                   VALUES ($1, $2, $3, $4, $5)""",
                action,
                user_email,
                table_name,
                uuid.UUID(record_id) if record_id else None,
                details,
            )
    except Exception as e:
        logger.warning(f"Audit log failed: {e}")


def file_type_from_mime(mime_type: str) -> str:
    if mime_type == "application/pdf":
        return "pdf"
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("text/") or mime_type == "application/json":
        return "text"
    return "other"
