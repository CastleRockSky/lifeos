"""
email_ingest.py — IMAP polling + email parsing + ingestion (Phase 3).

Polls a configured IMAP mailbox for new (UNSEEN) messages. For each message:
  1. Parse headers, body, and attachments.
  2. Deduplicate by RFC 5322 Message-ID.
  3. Look up the sender in email_sender_map for domain/category/subject hints.
  4. Run each attachment through the shared ingestion pipeline (ingest_file).
  5. If there are no attachments but the body contains useful text, store
     the body as a text document.
  6. Update the email_messages row with final status and document count.

Failed messages are kept (status='failed') and surfaced via /api/email/queue
for retry. Successful messages get marked \\Seen (and optionally labelled on
Gmail) so they aren't reprocessed.
"""

import asyncio
import email
import logging
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime
from typing import Optional

from config import get_settings
from database import get_pool
from ingest import ingest_file

logger = logging.getLogger(__name__)


# ── Module-level stats (for /api/email/status) ──────────────────────────

_stats = {
    "running": False,
    "last_poll_at": None,
    "last_connect_at": None,
    "last_error": None,
    "last_error_at": None,
    "messages_processed": 0,
    "messages_failed": 0,
    "documents_created": 0,
    "last_message_at": None,
    "last_message_subject": None,
}


def get_email_stats() -> dict:
    settings = get_settings()
    return {
        **_stats,
        "imap_enabled": settings.imap_enabled,
        "imap_host": settings.imap_host,
        "imap_username": settings.imap_username,
        "imap_mailbox": settings.imap_mailbox,
        "poll_interval": settings.imap_poll_interval,
    }


# ── Header / address helpers ────────────────────────────────────────────

def _decode(value: Optional[str]) -> str:
    """Decode an RFC 2047 encoded header value to a plain str."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


_SUBJECT_PREFIX_RE = re.compile(
    r"^\s*(re|fwd?|fw)\s*(\[\d+\])?\s*:\s*",
    re.IGNORECASE,
)


def _clean_subject(subject: str) -> str:
    """Strip leading Re:/Fwd:/Fw: prefixes (possibly stacked)."""
    s = subject or ""
    for _ in range(8):  # cap iterations
        new = _SUBJECT_PREFIX_RE.sub("", s, count=1)
        if new == s:
            break
        s = new
    return s.strip()


_FORWARDED_FROM_RE = re.compile(
    r"^\s*(?:From|De|Von|発信者)\s*:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)
_EMAIL_IN_LINE_RE = re.compile(r"<\s*([^<>\s,]+@[^<>\s,]+)\s*>")
_BARE_EMAIL_RE = re.compile(r"([\w.+-]+@[\w.-]+\.[A-Za-z]{2,})")


def _extract_original_sender(body_text: str) -> Optional[str]:
    """For forwarded mail, pull the original 'From:' from the quoted body."""
    if not body_text:
        return None
    # Look at the first ~3000 chars; the forwarded header is usually near the top
    head = body_text[:3000]
    for match in _FORWARDED_FROM_RE.finditer(head):
        line = match.group(1).strip()
        m = _EMAIL_IN_LINE_RE.search(line) or _BARE_EMAIL_RE.search(line)
        if m:
            return m.group(1).lower()
    return None


def _first_address(value: str) -> str:
    """Pull the first email address out of a header value."""
    if not value:
        return ""
    addrs = getaddresses([value])
    if addrs:
        _, addr = addrs[0]
        return (addr or "").lower().strip()
    return ""


# ── Email parsing ───────────────────────────────────────────────────────

def parse_email(raw_bytes: bytes) -> dict:
    """Parse a raw RFC 822 message into a structured dict.

    Returns:
        {
          "message_id": str | None,
          "sender": str,
          "recipient": str,
          "subject": str,
          "clean_subject": str,
          "body_text": str,
          "body_html": str,
          "received_at": datetime | None,
          "original_sender": str | None,
          "attachments": [ {filename, content_type, data: bytes}, ... ],
        }
    """
    msg: Message = email.message_from_bytes(raw_bytes)

    subject = _decode(msg.get("Subject"))
    sender = _first_address(_decode(msg.get("From")))
    recipient = _first_address(_decode(msg.get("To")))
    message_id = (msg.get("Message-ID") or "").strip() or None

    received_at = None
    date_hdr = msg.get("Date")
    if date_hdr:
        try:
            received_at = parsedate_to_datetime(date_hdr)
            if received_at and received_at.tzinfo is None:
                received_at = received_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

    body_text_parts: list[str] = []
    body_html_parts: list[str] = []
    attachments: list[dict] = []

    for part in msg.walk():
        if part.is_multipart():
            continue

        content_type = (part.get_content_type() or "").lower()
        disposition = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        if filename:
            filename = _decode(filename)

        is_attachment = (
            "attachment" in disposition
            or filename
            or (content_type.startswith("image/") and "inline" in disposition)
        )

        if is_attachment and filename:
            try:
                data = part.get_payload(decode=True) or b""
            except Exception as e:
                logger.warning(f"Failed to decode attachment {filename}: {e}")
                continue
            if not data:
                continue
            attachments.append({
                "filename": filename,
                "content_type": content_type,
                "data": data,
            })
            continue

        if content_type == "text/plain":
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                body_text_parts.append(payload.decode(charset, errors="replace"))
            except LookupError:
                body_text_parts.append(payload.decode("utf-8", errors="replace"))
        elif content_type == "text/html":
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                body_html_parts.append(payload.decode(charset, errors="replace"))
            except LookupError:
                body_html_parts.append(payload.decode("utf-8", errors="replace"))

    body_text = "\n\n".join(body_text_parts).strip()
    body_html = "\n\n".join(body_html_parts).strip()

    return {
        "message_id": message_id,
        "sender": sender,
        "recipient": recipient,
        "subject": subject,
        "clean_subject": _clean_subject(subject),
        "body_text": body_text,
        "body_html": body_html,
        "received_at": received_at,
        "original_sender": _extract_original_sender(body_text),
        "attachments": attachments,
    }


# ── Sender mapping ──────────────────────────────────────────────────────

def _sender_matches_pattern(sender: str, pattern: str) -> bool:
    """Match a sender address against an exact address or a *@domain pattern."""
    if not sender or not pattern:
        return False
    sender = sender.lower()
    pattern = pattern.lower().strip()
    if pattern.startswith("*@"):
        return sender.endswith("@" + pattern[2:])
    return sender == pattern


async def lookup_sender_map(sender: str, original_sender: Optional[str]) -> dict:
    """Find the best matching sender_map row.

    Tries the original (forwarded) sender first, then the forwarder. Exact
    address matches beat wildcard matches.
    """
    pool = get_pool()
    candidates = [s for s in (original_sender, sender) if s]
    if not candidates:
        return {}

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT sender_pattern, domain, category, subject_hint, confidence "
            "FROM email_sender_map ORDER BY (sender_pattern LIKE '*@%') ASC, confidence DESC"
        )

    for cand in candidates:
        for row in rows:
            if _sender_matches_pattern(cand, row["sender_pattern"]):
                return {
                    "matched_pattern": row["sender_pattern"],
                    "matched_address": cand,
                    "domain": row["domain"],
                    "category": row["category"],
                    "subject_hint": row["subject_hint"],
                    "confidence": float(row["confidence"] or 0.0),
                }
    return {}


async def learn_sender_mapping(
    sender: str,
    *,
    domain: Optional[str],
    category: Optional[str],
    subject_hint: Optional[str],
) -> None:
    """After AI analysis succeeds with high confidence, record the sender→domain mapping.

    Upserts on sender_pattern (exact-address form). Confidence climbs with repeated
    matches, but only when the AI keeps agreeing.
    """
    if not sender or not domain:
        return
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO email_sender_map
                (sender_pattern, domain, category, subject_hint,
                 auto_learned, confidence, match_count, last_matched_at)
            VALUES ($1, $2, $3, $4, true, 0.5, 1, NOW())
            ON CONFLICT (sender_pattern) DO UPDATE SET
                match_count = email_sender_map.match_count + 1,
                last_matched_at = NOW(),
                domain = CASE
                    WHEN email_sender_map.domain IS NULL THEN EXCLUDED.domain
                    ELSE email_sender_map.domain
                END,
                category = CASE
                    WHEN email_sender_map.category IS NULL THEN EXCLUDED.category
                    ELSE email_sender_map.category
                END,
                subject_hint = COALESCE(email_sender_map.subject_hint, EXCLUDED.subject_hint),
                confidence = LEAST(0.99, COALESCE(email_sender_map.confidence, 0) + 0.1)
        """, sender.lower(), domain, category, subject_hint)


# ── Email processing ────────────────────────────────────────────────────

# Reasonable cap so we don't write entire newsletters as documents.
_MIN_BODY_DOCUMENT_CHARS = 200
_MAX_BODY_DOCUMENT_CHARS = 50000


async def _save_attachment_to_temp(att: dict) -> str:
    """Write an attachment payload to a temp file and return its path."""
    safe_name = re.sub(r"[^\w.\-]+", "_", att["filename"]) or "attachment.bin"
    fd, path = tempfile.mkstemp(prefix="email_", suffix="_" + safe_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(att["data"])
    except Exception:
        os.close(fd)
        raise
    return path


def _format_email_as_text(parsed: dict) -> str:
    """Build a plain-text 'document' from an email body when there are no attachments."""
    lines = [
        f"From: {parsed.get('sender', '')}",
        f"To: {parsed.get('recipient', '')}",
        f"Subject: {parsed.get('subject', '')}",
    ]
    if parsed.get("original_sender"):
        lines.append(f"Original sender: {parsed['original_sender']}")
    if parsed.get("received_at"):
        lines.append(f"Date: {parsed['received_at'].isoformat()}")
    lines.append("")
    body = parsed.get("body_text") or ""
    lines.append(body[:_MAX_BODY_DOCUMENT_CHARS])
    return "\n".join(lines)


async def process_email(raw_bytes: bytes) -> dict:
    """Parse and ingest a single email. Returns the email_messages row id and outcome.

    Idempotent on Message-ID: a duplicate forward returns the prior row without
    re-ingesting attachments.
    """
    pool = get_pool()
    parsed = parse_email(raw_bytes)

    # Dedup by Message-ID (when present)
    if parsed["message_id"]:
        async with pool.acquire() as conn:
            existing = await conn.fetchrow(
                "SELECT id, status, document_count FROM email_messages WHERE message_id = $1",
                parsed["message_id"],
            )
        if existing:
            logger.info(f"Email dedup: {parsed['message_id']} already ingested ({existing['status']})")
            return {
                "id": str(existing["id"]),
                "status": existing["status"],
                "document_count": existing["document_count"],
                "duplicate": True,
            }

    sender_map = await lookup_sender_map(parsed["sender"], parsed["original_sender"])

    # Insert email_messages row in 'processing' state.
    async with pool.acquire() as conn:
        email_row_id = await conn.fetchval("""
            INSERT INTO email_messages (
                message_id, sender, original_sender, recipient,
                subject, clean_subject, body_text, body_html, received_at,
                attachment_count, status, raw_size_bytes,
                domain_hint, category_hint, subject_hint
            ) VALUES (
                $1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                'processing', $11, $12, $13, $14
            )
            RETURNING id
        """,
            parsed["message_id"],
            parsed["sender"],
            parsed["original_sender"],
            parsed["recipient"],
            parsed["subject"],
            parsed["clean_subject"],
            parsed["body_text"],
            parsed["body_html"],
            parsed["received_at"],
            len(parsed["attachments"]),
            len(raw_bytes),
            sender_map.get("domain"),
            sender_map.get("category"),
            sender_map.get("subject_hint"),
        )

    docs_created: list[str] = []
    errors: list[str] = []

    domain_hint = sender_map.get("domain")
    category_hint = sender_map.get("category")
    subject_hint = sender_map.get("subject_hint")

    # Tags applied to every doc from this email — useful for review queue filters.
    base_tags = ["email"]
    if parsed["sender"]:
        base_tags.append(f"from:{parsed['sender']}")

    # Compose a single-line "context" for AI analysis (subject + sender)
    # We don't have a slot in ingest_file for this, so we prepend it to the title
    # only as a fallback if AI doesn't generate a better one. Sender is also
    # surfaced via tags above.

    # ── Attachments ──────────────────────────────────────────────────────
    for att in parsed["attachments"]:
        tmp_path = None
        try:
            tmp_path = await _save_attachment_to_temp(att)
            result = await ingest_file(
                tmp_path,
                original_filename=att["filename"],
                title=att["filename"],
                domain=domain_hint,
                category=category_hint,
                tags=list(base_tags),
                source="email_forward",
            )
            doc_id = result["id"]
            if result.get("skipped"):
                logger.info(f"Email attachment was an exact duplicate of {doc_id}; not re-linking")
            else:
                docs_created.append(doc_id)
                # Link doc to the email row
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE documents SET email_message_id = $1 WHERE id = $2",
                        email_row_id, uuid.UUID(doc_id),
                    )
        except Exception as e:
            logger.exception(f"Email attachment failed: {att.get('filename')}: {e}")
            errors.append(f"{att.get('filename')}: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # ── Text-only fallback ──────────────────────────────────────────────
    body_text = parsed.get("body_text") or ""
    if not parsed["attachments"] and len(body_text.strip()) >= _MIN_BODY_DOCUMENT_CHARS:
        tmp_path = None
        try:
            text = _format_email_as_text(parsed)
            fd, tmp_path = tempfile.mkstemp(prefix="email_body_", suffix=".txt")
            with os.fdopen(fd, "wb") as f:
                f.write(text.encode("utf-8"))

            title = parsed["clean_subject"] or "Email message"
            # Keep filename predictable for storage
            safe_name = re.sub(r"[^\w.\-]+", "_", title)[:80] or "email"
            result = await ingest_file(
                tmp_path,
                original_filename=f"{safe_name}.txt",
                title=title,
                domain=domain_hint,
                category=category_hint,
                tags=list(base_tags),
                source="email_forward",
            )
            doc_id = result["id"]
            if result.get("skipped"):
                logger.info(f"Email body was an exact duplicate of {doc_id}; not re-linking")
            else:
                docs_created.append(doc_id)
                async with pool.acquire() as conn:
                    await conn.execute(
                        "UPDATE documents SET email_message_id = $1 WHERE id = $2",
                        email_row_id, uuid.UUID(doc_id),
                    )
        except Exception as e:
            logger.exception(f"Email body-as-doc failed: {e}")
            errors.append(f"body: {e}")
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # ── Final status ────────────────────────────────────────────────────
    if docs_created and not errors:
        status = "processed"
    elif docs_created and errors:
        status = "partial"
    elif not docs_created and not parsed["attachments"] and not body_text.strip():
        status = "processed"  # empty email, nothing to do
    else:
        status = "failed"

    error_message = "; ".join(errors)[:1000] if errors else None

    async with pool.acquire() as conn:
        await conn.execute("""
            UPDATE email_messages SET
                document_count = $2,
                status = $3,
                error_message = $4,
                processed_at = NOW()
            WHERE id = $1
        """, email_row_id, len(docs_created), status, error_message)

    _stats["messages_processed" if status != "failed" else "messages_failed"] += 1
    _stats["documents_created"] += len(docs_created)
    _stats["last_message_at"] = datetime.now(timezone.utc).isoformat()
    _stats["last_message_subject"] = parsed["subject"][:200] if parsed["subject"] else None

    return {
        "id": str(email_row_id),
        "status": status,
        "document_count": len(docs_created),
        "documents": docs_created,
        "errors": errors,
        "duplicate": False,
    }


async def retry_email(email_message_id: str) -> dict:
    """Re-run ingestion for a previously failed email.

    Without the original raw bytes we can't redo attachment processing, so for
    now retry only re-evaluates a body-only email or surfaces a clear error.
    A future enhancement is to keep raw email bodies on disk for true retry.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, attachment_count, body_text FROM email_messages WHERE id = $1",
            uuid.UUID(email_message_id),
        )
    if not row:
        return {"ok": False, "error": "not_found"}

    if row["attachment_count"] and row["attachment_count"] > 0:
        # We didn't persist attachment bytes; user must re-forward the email.
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE email_messages SET retry_count = retry_count + 1 WHERE id = $1",
                uuid.UUID(email_message_id),
            )
        return {
            "ok": False,
            "error": "attachments_not_replayable",
            "message": "Re-forward the original email to retry attachments.",
        }

    # Body-only retry: build a synthetic raw email from the stored body.
    async with pool.acquire() as conn:
        full = await conn.fetchrow(
            "SELECT message_id, sender, recipient, subject, body_text, received_at "
            "FROM email_messages WHERE id = $1",
            uuid.UUID(email_message_id),
        )

    msg = email.message.EmailMessage()
    if full["message_id"]:
        msg["Message-ID"] = full["message_id"]
    msg["From"] = full["sender"] or ""
    msg["To"] = full["recipient"] or ""
    msg["Subject"] = full["subject"] or ""
    if full["received_at"]:
        msg["Date"] = email.utils.format_datetime(full["received_at"])
    msg.set_content(full["body_text"] or "")

    # Mark previous row failed→retrying then process: but process_email dedups
    # on Message-ID, so first clear the old row.
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM email_messages WHERE id = $1",
            uuid.UUID(email_message_id),
        )

    result = await process_email(msg.as_bytes())
    return {"ok": True, **result}


# ── IMAP polling loop ───────────────────────────────────────────────────

async def _poll_once(client) -> int:
    """Poll the mailbox once. Returns the number of messages processed."""
    settings = get_settings()
    processed = 0

    # SELECT (re-select on every poll: keeps the session healthy)
    typ, _ = await client.select(settings.imap_mailbox)
    if typ != "OK":
        raise RuntimeError(f"IMAP SELECT failed: {typ}")

    typ, data = await client.uid_search("UNSEEN")
    if typ != "OK":
        raise RuntimeError(f"IMAP SEARCH failed: {typ}")

    # data is a list whose first element is a space-separated UID string
    uid_blob = b""
    if data:
        uid_blob = data[0] if isinstance(data[0], (bytes, bytearray)) else str(data[0]).encode()
    uids = [u for u in uid_blob.split() if u]

    if not uids:
        return 0

    logger.info(f"IMAP: {len(uids)} new message(s) to process")

    for uid in uids:
        uid_str = uid.decode() if isinstance(uid, bytes) else str(uid)
        try:
            typ, msg_data = await client.uid("fetch", uid_str, "(RFC822)")
            if typ != "OK" or not msg_data:
                logger.warning(f"IMAP: fetch failed for UID {uid_str}: {typ}")
                continue

            raw = _extract_rfc822_payload(msg_data)
            if not raw:
                logger.warning(f"IMAP: no RFC822 payload for UID {uid_str}")
                continue

            if len(raw) > settings.imap_max_message_size:
                logger.warning(
                    f"IMAP: skipping UID {uid_str}, size {len(raw)} exceeds cap "
                    f"{settings.imap_max_message_size}"
                )
                # Mark seen so we don't re-fetch every poll
                await client.uid("store", uid_str, "+FLAGS", "(\\Seen)")
                continue

            result = await process_email(raw)

            # Mark \Seen and (if Gmail) apply a label
            await client.uid("store", uid_str, "+FLAGS", "(\\Seen)")
            label = (
                settings.imap_processed_label
                if result["status"] in ("processed", "partial")
                else settings.imap_failed_label
            )
            if label:
                # X-GM-LABELS is a Gmail extension; harmless on other servers
                # (will return BAD which we ignore).
                try:
                    await client.uid(
                        "store", uid_str, "+X-GM-LABELS", f'("{label}")'
                    )
                except Exception:
                    pass

            processed += 1
        except Exception as e:
            logger.exception(f"IMAP: error processing UID {uid_str}: {e}")
            _stats["last_error"] = str(e)
            _stats["last_error_at"] = datetime.now(timezone.utc).isoformat()

    return processed


def _extract_rfc822_payload(fetch_response) -> Optional[bytes]:
    """Pull the RFC822 bytes out of an aioimaplib fetch response.

    aioimaplib returns a list whose elements alternate between header lines
    (e.g. b'1 FETCH (UID 42 RFC822 {12345}') and the literal payload bytes.
    """
    for item in fetch_response:
        if isinstance(item, (bytes, bytearray)):
            # Skip the short header lines; the actual message is always large
            # and starts with header fields like 'Return-Path:' or similar.
            if len(item) > 200 or b"\r\n" in item[:200]:
                # Heuristic: the payload is the longest bytes segment.
                pass
    # Pick the longest bytes payload — that's the RFC822 body.
    payloads = [b for b in fetch_response if isinstance(b, (bytes, bytearray))]
    if not payloads:
        return None
    return bytes(max(payloads, key=len))


async def watch_imap():
    """Long-running IMAP polling loop. Reconnects on transient errors."""
    settings = get_settings()
    if not settings.imap_enabled:
        logger.info("IMAP watcher disabled (imap_enabled=false)")
        return

    if not (settings.imap_host and settings.imap_username and settings.imap_password):
        logger.warning("IMAP watcher: missing credentials; staying idle")
        return

    # Imported here so the dependency is optional until imap_enabled flips on.
    import aioimaplib

    _stats["running"] = True
    logger.info(
        f"IMAP watcher started: {settings.imap_username}@{settings.imap_host} "
        f"poll={settings.imap_poll_interval}s"
    )

    backoff = settings.imap_poll_interval
    try:
        while True:
            client = None
            try:
                if settings.imap_use_ssl:
                    client = aioimaplib.IMAP4_SSL(
                        host=settings.imap_host, port=settings.imap_port
                    )
                else:
                    client = aioimaplib.IMAP4(
                        host=settings.imap_host, port=settings.imap_port
                    )
                await client.wait_hello_from_server()
                typ, _ = await client.login(settings.imap_username, settings.imap_password)
                if typ != "OK":
                    raise RuntimeError(f"IMAP login failed: {typ}")
                _stats["last_connect_at"] = datetime.now(timezone.utc).isoformat()
                backoff = settings.imap_poll_interval

                while True:
                    try:
                        n = await _poll_once(client)
                        _stats["last_poll_at"] = datetime.now(timezone.utc).isoformat()
                        if n:
                            logger.info(f"IMAP: processed {n} message(s)")
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.error(f"IMAP poll error: {e}")
                        _stats["last_error"] = str(e)
                        _stats["last_error_at"] = datetime.now(timezone.utc).isoformat()
                        # Break inner loop to reconnect
                        break
                    await asyncio.sleep(settings.imap_poll_interval)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"IMAP connection error: {e}")
                _stats["last_error"] = str(e)
                _stats["last_error_at"] = datetime.now(timezone.utc).isoformat()
            finally:
                if client is not None:
                    try:
                        await client.logout()
                    except Exception:
                        pass

            # Capped exponential backoff between reconnects
            await asyncio.sleep(min(backoff, 600))
            backoff = min(backoff * 2, 600)

    except asyncio.CancelledError:
        _stats["running"] = False
        logger.info("IMAP watcher stopped")
        raise
