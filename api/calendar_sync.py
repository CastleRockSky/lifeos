"""
calendar_sync.py — Google Calendar bidirectional sync (Phase 10).

Action items with a due_date get mirrored to a Google Calendar. The mapping
is one-to-one: action_items.calendar_event_id stores the Google event id.

The module is dormant until OAuth tokens exist at the configured path.
Run scripts/bootstrap_google_calendar.py once to create them.
"""

import asyncio
import json
import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

from config import get_settings

logger = logging.getLogger(__name__)


SCOPES = ["https://www.googleapis.com/auth/calendar.events"]


def _is_enabled() -> bool:
    s = get_settings()
    return bool(
        s.google_calendar_enabled
        and s.google_credentials_path
        and os.path.exists(s.google_credentials_path)
    )


def _allowed_domain(domain: Optional[str]) -> bool:
    s = get_settings()
    raw = (s.google_calendar_domains or "").strip()
    if not raw:
        return True
    allowed = {d.strip().lower() for d in raw.split(",") if d.strip()}
    return (domain or "").lower() in allowed


def _build_service():
    """Construct an authorised Google Calendar service. Raises on auth failure."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from google.auth.transport.requests import Request

    s = get_settings()
    creds = Credentials.from_authorized_user_file(s.google_credentials_path, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open(s.google_credentials_path, "w") as f:
                f.write(creds.to_json())
        else:
            raise RuntimeError("Google Calendar credentials invalid; re-run bootstrap")
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _format_event(action: dict) -> dict:
    s = get_settings()
    title = (s.google_calendar_event_prefix or "") + (action.get("title") or "Action")
    desc_parts = []
    if action.get("description"):
        desc_parts.append(action["description"])
    if action.get("priority"):
        desc_parts.append(f"Priority: {action['priority']}")
    if action.get("subject_name"):
        desc_parts.append(f"Subject: {action['subject_name']}")
    if s.google_calendar_link_base and action.get("id"):
        desc_parts.append(f"\n{s.google_calendar_link_base}/actions/{action['id']}")
    description = "\n".join(desc_parts)

    due = action.get("due_date")
    if isinstance(due, datetime):
        # Timed event
        end = due + timedelta(hours=1)
        body = {
            "summary": title,
            "description": description,
            "start": {"dateTime": due.isoformat(), "timeZone": "America/Denver"},
            "end": {"dateTime": end.isoformat(), "timeZone": "America/Denver"},
        }
    else:
        # All-day event. Google's end date is exclusive.
        d = due if isinstance(due, date) else date.fromisoformat(str(due))
        body = {
            "summary": title,
            "description": description,
            "start": {"date": d.isoformat()},
            "end": {"date": (d + timedelta(days=1)).isoformat()},
        }

    if action.get("recurrence_rule") == "monthly":
        body["recurrence"] = ["RRULE:FREQ=MONTHLY;COUNT=12"]

    return body


# ── Sync operations (sync, called from background tasks) ────────────────

def _create_event_sync(action: dict) -> Optional[str]:
    s = get_settings()
    service = _build_service()
    body = _format_event(action)
    event = service.events().insert(calendarId=s.google_calendar_id, body=body).execute()
    return event.get("id")


def _update_event_sync(action: dict, event_id: str) -> Optional[str]:
    s = get_settings()
    service = _build_service()
    body = _format_event(action)
    try:
        event = service.events().update(
            calendarId=s.google_calendar_id, eventId=event_id, body=body,
        ).execute()
        return event.get("id")
    except Exception as e:
        # If the event was deleted server-side, fall back to creating a new one.
        msg = str(e).lower()
        if "not found" in msg or "deleted" in msg:
            return _create_event_sync(action)
        raise


def _delete_event_sync(event_id: str) -> None:
    s = get_settings()
    service = _build_service()
    try:
        service.events().delete(
            calendarId=s.google_calendar_id, eventId=event_id, sendUpdates="none"
        ).execute()
    except Exception as e:
        msg = str(e).lower()
        if "not found" in msg or "deleted" in msg:
            return
        raise


# ── Public async API (offloads to a thread, never raises) ───────────────

async def sync_action_create(action: dict) -> Optional[str]:
    """Create a calendar event for a new action item; return event_id or None."""
    if not _is_enabled() or not action.get("due_date"):
        return None
    if not _allowed_domain(action.get("domain")):
        return None
    try:
        return await asyncio.to_thread(_create_event_sync, action)
    except Exception as e:
        logger.warning(f"Calendar create failed for action {action.get('id')}: {e}")
        return None


async def sync_action_update(action: dict, event_id: Optional[str]) -> Optional[str]:
    """Update or create a calendar event for an action; return effective event_id."""
    if not _is_enabled():
        return event_id
    if not _allowed_domain(action.get("domain")):
        return event_id
    if not action.get("due_date"):
        # Due date was removed — drop the event.
        if event_id:
            await sync_action_delete(event_id)
        return None
    try:
        if event_id:
            return await asyncio.to_thread(_update_event_sync, action, event_id)
        return await asyncio.to_thread(_create_event_sync, action)
    except Exception as e:
        logger.warning(f"Calendar update failed for action {action.get('id')}: {e}")
        return event_id


async def sync_action_delete(event_id: str) -> None:
    """Delete the calendar event for a completed/dismissed action."""
    if not _is_enabled() or not event_id:
        return
    try:
        await asyncio.to_thread(_delete_event_sync, event_id)
    except Exception as e:
        logger.warning(f"Calendar delete failed for event {event_id}: {e}")


def status() -> dict:
    s = get_settings()
    return {
        "enabled": s.google_calendar_enabled,
        "configured": bool(s.google_credentials_path and os.path.exists(s.google_credentials_path)),
        "calendar_id": s.google_calendar_id,
        "domains_filter": s.google_calendar_domains or "all",
        "credentials_path": s.google_credentials_path,
    }
