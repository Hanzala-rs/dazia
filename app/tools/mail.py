"""Read-only Gmail tool. Returns sender/subject/preview of inbox messages; never sends or changes mail.

The Gmail client is created once on first use. Email text in the result is untrusted data.
"""
import logging
import sqlite3
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.config import settings
from app.services.gmail import GmailClient, GmailError
from app.tools import ToolResult

log = logging.getLogger(__name__)

QUERIES = {"unread": "in:inbox is:unread", "recent": "in:inbox"}
_client: GmailClient | None = None


class SummarizeEmailArgs(BaseModel):
    filter: Literal["unread", "recent"] = "unread"
    limit: int = Field(default=5, ge=1, le=10)


def _get_client() -> GmailClient:
    global _client
    if _client is None:
        _client = GmailClient(settings.gmail_token_file, settings.gmail_timeout_s)
    return _client


def _format_local(value: datetime) -> str:
    hour12 = value.hour % 12 or 12
    am_pm = "AM" if value.hour < 12 else "PM"
    return f"{value:%a} {value.day} {value:%b}, {hour12}:{value:%M} {am_pm}"


def summarize_email(conn: sqlite3.Connection, args: SummarizeEmailArgs, now: datetime, tz: str) -> ToolResult:
    """data={"emails": [{from, subject, received_local, snippet, unread}, ...]}. `conn` is unused."""
    if not settings.gmail_enabled:
        return ToolResult(ok=False, message="Gmail isn't enabled. Set DAZIA_GMAIL_ENABLED=true in .env and restart.")
    try:
        emails = _get_client().list_messages(QUERIES[args.filter], args.limit)
    except GmailError as exc:
        log.warning("Gmail read failed: %s", exc.__class__.__name__)
        return ToolResult(ok=False, message=str(exc))
    log.info("Gmail returned %d message(s)", len(emails))  # counts only, never content

    label = "unread emails" if args.filter == "unread" else "emails in your inbox"
    if not emails:
        return ToolResult(ok=True, message=f"You have no {label}.", data={"emails": []})

    zone = ZoneInfo(tz)
    items = [
        {
            "from": e.sender,
            "subject": e.subject,
            "received_local": e.received.astimezone(zone).isoformat(),
            "snippet": e.snippet,
            "unread": e.unread,
        }
        for e in emails
    ]
    lines = [f"- {e.sender}: {e.subject} ({_format_local(e.received.astimezone(zone))})" for e in emails]
    heading = f"Latest {len(emails)} {label}:"
    return ToolResult(ok=True, message="\n".join([heading, *lines]), data={"emails": items})
