"""Draft-and-confirm email (Phase 12). Drafting only: nothing here sends.

The LLM may propose draft_email / draft_reply. The orchestrator turns that into a Draft, shows it in full,
and sends it only when the user's next message is exactly a confirmation word (see assistant.py).
Recipients are never guessed: a new email needs a literal address; a reply targets a numbered email
from the last list DAZIA showed, and goes to that email's sender (or its reply-to address).
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import BaseModel, Field

DRAFT_TTL = timedelta(minutes=10)
CONFIRM_WORDS = {"send", "send it"}
CANCEL_WORDS = {"cancel", "discard", "don't send", "dont send", "no"}
# One plain address: no names, lists, or angle brackets, so the recipient shown is exactly the one used.
_ADDRESS = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")


class DraftEmailArgs(BaseModel):
    to: str = Field(min_length=3, max_length=254)  # must be a full email address
    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1, max_length=5000)


class DraftReplyArgs(BaseModel):
    email_number: int = Field(ge=1, le=10)  # number from the last email list
    body: str = Field(min_length=1, max_length=5000)


@dataclass
class Draft:
    kind: str  # "new" | "reply"
    to: str  # what the user is shown as the recipient
    subject: str
    body: str
    created: datetime
    message_id: str = ""  # reply only

    def expired(self, now: datetime) -> bool:
        return now - self.created > DRAFT_TTL


def normalize(message: str) -> str:
    """Lower-case and drop surrounding punctuation, so 'Send.' or 'send!' (e.g. from voice) still match."""
    return " ".join(message.lower().strip(" .!?,'\"").split())


def new_draft(args: DraftEmailArgs, now: datetime) -> Draft | str:
    """A Draft, or a question for the user when the recipient isn't one exact address."""
    to = args.to.strip()
    if not _ADDRESS.match(to):
        return ("Who should I send it to? Please give the full email address (for example ali@example.com). "
                "I don't guess recipients.")
    return Draft("new", to, _one_line(args.subject), args.body.strip(), now)


def reply_draft(args: DraftReplyArgs, last_emails: list[dict], now: datetime) -> Draft | str:
    """`last_emails` = the Outlook emails from the last list shown, in the same order as their numbers."""
    if not last_emails:
        return "Which email should I reply to? Ask me to show your Outlook emails first, then use its number."
    if args.email_number > len(last_emails):
        return f"I only listed {len(last_emails)} email(s). Which number should I reply to?"
    email = last_emails[args.email_number - 1]
    to = email["reply_to"] or email["from"]
    subject = email["subject"] if email["subject"].lower().startswith("re:") else f"RE: {email['subject']}"
    return Draft("reply", to, subject, args.body.strip(), now, message_id=email["id"])


def preview(draft: Draft) -> str:
    what = "Reply" if draft.kind == "reply" else "Email"
    return "\n".join([
        f"{what} draft — not sent yet.",
        f"To: {draft.to}",
        f"Subject: {draft.subject}",
        "",
        draft.body,
        "",
        'Type "send" to send it, or "cancel" to discard it.',
    ])


def _one_line(text: str) -> str:
    return " ".join(text.split())
