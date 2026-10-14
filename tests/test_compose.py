"""Phase 12: draft-and-confirm. The LLM can only draft; sending needs the user's exact "send"."""
from datetime import timedelta

import httpx
import pytest

from app import assistant as assistant_mod
from app.services import outlook
from app.tools import ToolResult, compose, mail
from tests.test_assistant import NOW, FakeLLM, env, make  # noqa: F401  (env is a fixture)

LISTED = {
    "account": "outlook",
    "emails": [
        {"from": "Ali <ali@x.com>", "reply_to": "", "subject": "Invoice", "snippet": "pay", "id": "m1",
         "received_local": "", "unread": True},
        {"from": "Shop <noreply@shop.com>", "reply_to": "help@shop.com", "subject": "RE: Order", "snippet": "",
         "id": "m2", "received_local": "", "unread": True},
    ],
}


class FakeOutlook:
    def __init__(self, error=None):
        self.sent, self.replies, self.error = [], [], error

    def send_mail(self, to, subject, body):
        if self.error:
            raise self.error
        self.sent.append((to, subject, body))

    def reply(self, message_id, body):
        self.replies.append((message_id, body))


@pytest.fixture
def outbox(env, monkeypatch):
    fake = FakeOutlook()
    monkeypatch.setattr(mail.settings, "outlook_enabled", True)
    monkeypatch.setattr(mail.settings, "outlook_send_enabled", True)
    monkeypatch.setattr(mail, "_get_client", lambda account: fake)

    def fake_summarize(conn, args, now, tz):
        return ToolResult(ok=True, message="Latest 2 unread emails (Outlook):\n1. Ali\n2. Shop", data=LISTED)

    monkeypatch.setitem(assistant_mod.TOOLS, "summarize_email", (mail.SummarizeEmailArgs, fake_summarize))
    return fake


DRAFT = {"tool": "draft_email", "args": {"to": "ali@x.com", "subject": "Late", "body": "Running 10 min late."},
         "reply": "ok"}


def test_draft_is_shown_in_full_and_not_sent(outbox):
    r = make(FakeLLM(DRAFT)).handle("email ali@x.com that I'm running late")
    assert outbox.sent == [] and r.tool == "draft_email"
    assert r.reply.splitlines()[:5] == ["Email draft — not sent yet.", "To: ali@x.com", "Subject: Late", "",
                                        "Running 10 min late."]
    assert 'Type "send"' in r.reply


@pytest.mark.parametrize("word", ["send", "Send.", "  SEND it! "])
def test_exact_send_sends_without_the_llm(outbox, word):
    llm = FakeLLM(DRAFT)  # only one scripted answer: a second LLM call would fail the test
    a = make(llm)
    a.handle("email ali")
    r = a.handle(word)
    assert outbox.sent == [("ali@x.com", "Late", "Running 10 min late.")]
    assert r.reply == "Sent to ali@x.com." and r.data == {"sent": True}
    assert a.draft is None and len(llm.calls) == 1


def test_send_twice_sends_once(outbox):
    a = make(FakeLLM(DRAFT, {"tool": "none", "args": {}, "reply": "Nothing to send."}))
    a.handle("email ali")
    a.handle("send")
    a.handle("send")  # no draft any more: goes to the LLM, which cannot send
    assert len(outbox.sent) == 1


def test_cancel_and_yes(outbox):
    a = make(FakeLLM(DRAFT))
    a.handle("email ali")
    assert "type \"send\"" in a.handle("yes").reply.lower()  # "yes" is not a confirmation
    assert a.draft is not None
    assert a.handle("cancel").reply == "Draft discarded. Nothing was sent."
    assert outbox.sent == [] and a.draft is None


def test_other_message_discards_draft(outbox):
    a = make(FakeLLM(DRAFT, {"tool": "none", "args": {}, "reply": "Hi!"}))
    a.handle("email ali")
    r = a.handle("what's the weather like")
    assert r.reply.startswith("(Your email draft was discarded; nothing was sent.)")
    assert a.draft is None and outbox.sent == []


def test_expired_draft_is_not_sent(outbox):
    now = [NOW]
    a = assistant_mod.Assistant(FakeLLM(DRAFT), db_path=":memory:", tz="Asia/Karachi", clock=lambda: now[0])
    a.handle("email ali")
    now[0] = NOW + compose.DRAFT_TTL + timedelta(seconds=1)
    assert "expired" in a.handle("send").reply and outbox.sent == []


@pytest.mark.parametrize("to", ["Ali", "ali", "ali@x.com, bob@y.com", "Ali <ali@x.com>", "ali@x"])
def test_recipient_is_never_guessed(outbox, to):
    r = make(FakeLLM({**DRAFT, "args": {**DRAFT["args"], "to": to}})).handle("email ali")
    assert r.needs_clarification and "full email address" in r.reply and outbox.sent == []


def test_reply_uses_numbered_email_and_reply_to(outbox):
    a = make(FakeLLM({"tool": "summarize_email", "args": {}, "reply": ""}, {"summary": "Two emails."},
                     {"tool": "draft_reply", "args": {"email_number": 2, "body": "Thanks!"}, "reply": ""}))
    a.handle("show my email")
    r = a.handle("reply to 2 saying thanks")
    assert "To: help@shop.com" in r.reply and "Subject: RE: Order" in r.reply  # reply-to, no double RE:
    a.handle("send")
    assert outbox.replies == [("m2", "Thanks!")] and outbox.sent == []


def test_reply_needs_a_listed_email(outbox):
    r = make(FakeLLM({"tool": "draft_reply", "args": {"email_number": 1, "body": "Hi"}, "reply": ""})).handle("reply")
    assert r.needs_clarification and "show your Outlook emails first" in r.reply


def test_sending_switched_off_means_no_draft(outbox, monkeypatch):
    monkeypatch.setattr(mail.settings, "outlook_send_enabled", False)
    a = make(FakeLLM(DRAFT))
    r = a.handle("email ali")
    assert r.reply == mail.SEND_OFF_MESSAGE and a.draft is None


def test_send_failure_is_reported_honestly(outbox):
    outbox.error = outlook.OutlookError("I couldn't confirm the email was sent (ConnectError). Check your Sent Items.")
    a = make(FakeLLM(DRAFT))
    a.handle("email ali")
    r = a.handle("send")
    assert r.data == {"sent": False} and "couldn't confirm" in r.reply


# ---- Outlook service: the actual Graph calls ------------------------------------------------

class FakeMSAL:
    def __init__(self, granted):
        self.granted = granted

    def get_accounts(self):
        return [{"username": "me@outlook.com"}]

    def acquire_token_silent(self, scopes, account):
        return {"access_token": "tok-" + scopes[0]} if set(scopes) <= self.granted else None


def _client(handler, tmp_path, granted=frozenset({"Mail.Read", "Mail.Send"})):
    return outlook.OutlookClient("cid", str(tmp_path / "t.bin"), transport=httpx.MockTransport(handler),
                                 app=FakeMSAL(granted))


def test_graph_send_and_reply_requests(tmp_path):
    seen = []
    client = _client(lambda req: seen.append(req) or httpx.Response(202), tmp_path)
    client.send_mail("ali@x.com", "Late", "Running late")
    client.reply("m2", "Thanks!")
    send, reply = seen
    assert send.method == "POST" and send.url.path == "/v1.0/me/sendMail"
    assert send.headers["Authorization"] == "Bearer tok-Mail.Send"
    assert b'"toRecipients":[{"emailAddress":{"address":"ali@x.com"}}]' in send.content.replace(b" ", b"")
    assert reply.url.path == "/v1.0/me/messages/m2/reply" and b'"comment"' in reply.content


def test_send_without_mail_send_consent(tmp_path):
    client = _client(lambda req: pytest.fail("must not call Graph"), tmp_path, granted=frozenset({"Mail.Read"}))
    with pytest.raises(outlook.OutlookNotConnected, match="Mail.Send"):
        client.send_mail("ali@x.com", "s", "b")


def test_list_keeps_reply_to(tmp_path):
    msg = {"id": "m9", "from": {"emailAddress": {"name": "Shop", "address": "noreply@shop.com"}},
           "replyTo": [{"emailAddress": {"address": "help@shop.com"}}], "subject": "Order",
           "receivedDateTime": "2026-10-14T04:00:00Z", "bodyPreview": "", "isRead": False}
    email = _client(lambda req: httpx.Response(200, json={"value": [msg]}), tmp_path).list_messages(True, 1)[0]
    assert (email.id, email.reply_to) == ("m9", "help@shop.com")
