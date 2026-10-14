"""Read-only email tool (Gmail or Outlook.com). Returns sender/subject/preview of inbox messages;
never sends or changes mail.

Each account's client is created once on first use. Email text in the result is untrusted data.
"""
import logging
import sqlite3
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from app.config import settings
from app.services.gmail import Email, GmailClient, GmailError
from app.services.outlook import OutlookClient, OutlookError
from app.tools.compose import Draft
from app.tools import ToolResult

log = logging.getLogger(__name__)

Account = Literal["outlook", "gmail"]
NAMES = {"outlook": "Outlook", "gmail": "Gmail"}
GMAIL_QUERIES = {"unread": "in:inbox is:unread", "recent": "in:inbox"}
_clients: dict[str, GmailClient | OutlookClient] = {}


class SummarizeEmailArgs(BaseModel):
    filter: Literal["unread", "recent"] = "unread"
    limit: int = Field(default=5, ge=1, le=10)
    account: Account | None = None  # None = the first enabled account (Outlook, then Gmail)


def _enabled_accounts() -> list[str]:
    return [a for a, on in (("outlook", settings.outlook_enabled), ("gmail", settings.gmail_enabled)) if on]


def _get_client(account: str) -> GmailClient | OutlookClient:
    if account not in _clients:
        if account == "outlook":
            _clients[account] = OutlookClient(
                settings.outlook_client_id, settings.outlook_token_file, settings.outlook_timeout_s
            )
        else:
            _clients[account] = GmailClient(settings.gmail_token_file, settings.gmail_timeout_s)
    return _clients[account]


def _fetch(account: str, args: SummarizeEmailArgs) -> list[Email]:
    client = _get_client(account)
    if account == "outlook":
        return client.list_messages(unread_only=args.filter == "unread", limit=args.limit)
    return client.list_messages(GMAIL_QUERIES[args.filter], args.limit)


def _format_local(value: datetime) -> str:
    hour12 = value.hour % 12 or 12
    am_pm = "AM" if value.hour < 12 else "PM"
    return f"{value:%a} {value.day} {value:%b}, {hour12}:{value:%M} {am_pm}"


def summarize_email(conn: sqlite3.Connection, args: SummarizeEmailArgs, now: datetime, tz: str) -> ToolResult:
    """data={"emails": [{from, subject, received_local, snippet, unread}, ...]}. `conn` is unused."""
    enabled = _enabled_accounts()
    if not enabled:
        return ToolResult(ok=False, message="No email account is enabled. Set DAZIA_OUTLOOK_ENABLED=true "
                                            "or DAZIA_GMAIL_ENABLED=true in .env and restart.")
    account = args.account or enabled[0]
    name = NAMES[account]
    if account not in enabled:
        return ToolResult(ok=False, message=f"{name} isn't enabled. Set DAZIA_{account.upper()}_ENABLED=true "
                                            "in .env and restart.")
    try:
        emails = _fetch(account, args)
    except (GmailError, OutlookError) as exc:
        log.warning("%s read failed: %s", name, exc.__class__.__name__)
        return ToolResult(ok=False, message=str(exc))
    log.info("%s returned %d message(s)", name, len(emails))  # counts only, never content

    label = "unread emails" if args.filter == "unread" else "emails in your inbox"
    if not emails:
        return ToolResult(ok=True, message=f"You have no {label} ({name}).", data={"emails": [], "account": account})

    zone = ZoneInfo(tz)
    items = [
        {
            "from": e.sender,
            "subject": e.subject,
            "received_local": e.received.astimezone(zone).isoformat(),
            "snippet": e.snippet,
            "unread": e.unread,
            "id": e.id,
            "reply_to": e.reply_to,
        }
        for e in emails
    ]
    lines = [f"{i}. {e.sender}: {e.subject} ({_format_local(e.received.astimezone(zone))})"
             for i, e in enumerate(emails, start=1)]
    heading = f"Latest {len(emails)} {label} ({name}):"
    return ToolResult(ok=True, message="\n".join([heading, *lines]), data={"emails": items, "account": account})


# ---- Phase 12: sending a confirmed draft (Outlook only) -----------------------------------
SEND_OFF_MESSAGE = ("Sending email is switched off. To allow it: add the Mail.Send permission in Entra, set "
                    "DAZIA_OUTLOOK_ENABLED=true and DAZIA_OUTLOOK_SEND_ENABLED=true, sign in again, and restart.")


def sending_enabled() -> bool:
    return settings.outlook_enabled and settings.outlook_send_enabled


def send_draft(draft: Draft) -> ToolResult:
    """Send a draft the user has just confirmed. Never called on the LLM's request."""
    if not sending_enabled():
        return ToolResult(ok=False, message=SEND_OFF_MESSAGE)
    client = _get_client("outlook")
    try:
        if draft.kind == "reply":
            client.reply(draft.message_id, draft.body)
        else:
            client.send_mail(draft.to, draft.subject, draft.body)
    except OutlookError as exc:
        log.warning("Sending failed: %s", exc.__class__.__name__)
        return ToolResult(ok=False, message=str(exc))
    log.info("Email sent (%s)", draft.kind)  # never the recipient or text
    return ToolResult(ok=True, message=f"Sent to {draft.to}.")
