import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.services import gmail, outlook
from app.tools import mail

TZ = "Asia/Karachi"
NOW = datetime(2026, 10, 14, 15, 5, tzinfo=ZoneInfo(TZ))
MESSAGE = {
    "id": "m1",
    "internalDate": "1791950400000",  # 2026-10-14 04:00 UTC
    "labelIds": ["INBOX", "UNREAD"],
    "snippet": "Hi, it&#39;s about   the\ninvoice",
    "payload": {"headers": [{"name": "From", "value": "Ali <ali@x.com>"}, {"name": "Subject", "value": ""}]},
}


class FakeCreds:
    def __init__(self):
        self.token, self.valid, self.refresh_token, self.refreshed = "old", True, "r", 0

    def refresh(self, request):
        self.token, self.refreshed = "new", self.refreshed + 1

    def to_json(self):
        return json.dumps({"token": self.token})


def make_client(handler, monkeypatch, tmp_path):
    client = gmail.GmailClient(str(tmp_path / "t.bin"), transport=httpx.MockTransport(handler))
    client._creds = FakeCreds()
    monkeypatch.setattr(gmail, "save_token", lambda path, data: None)
    return client


def test_list_messages_parses_metadata_and_cleans_text(monkeypatch, tmp_path):
    seen = []

    def handler(req):
        seen.append(req)
        if req.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(200, json=MESSAGE)

    emails = make_client(handler, monkeypatch, tmp_path).list_messages("in:inbox is:unread", 5)
    assert len(emails) == 1
    e = emails[0]
    assert e.sender == "Ali <ali@x.com>" and e.subject == "(no subject)" and e.unread
    assert e.snippet == "Hi, it's about the invoice"
    assert e.received.isoformat() == "2026-10-14T04:00:00+00:00"
    assert seen[0].url.params["q"] == "in:inbox is:unread" and seen[0].url.params["maxResults"] == "5"
    assert seen[1].url.params["format"] == "metadata"  # never the full body
    assert seen[0].headers["Authorization"] == "Bearer old"
    assert all(r.method == "GET" for r in seen)


def test_expired_token_is_refreshed_once(monkeypatch, tmp_path):
    def handler(req):
        if req.headers["Authorization"] == "Bearer old":
            return httpx.Response(401)
        return httpx.Response(200, json={})

    client = make_client(handler, monkeypatch, tmp_path)
    assert client.list_messages("in:inbox", 3) == []
    assert client._creds.refreshed == 1


def test_permission_error_is_reported(monkeypatch, tmp_path):
    client = make_client(lambda req: httpx.Response(403), monkeypatch, tmp_path)
    with pytest.raises(gmail.GmailError, match="refused"):
        client.list_messages("in:inbox", 3)


def test_missing_token_means_not_connected(tmp_path):
    with pytest.raises(gmail.GmailNotConnected):
        gmail.load_token(tmp_path / "none.bin")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI is Windows-only")
def test_token_roundtrip_is_encrypted(tmp_path):
    path = tmp_path / "t.bin"
    gmail.save_token(path, '{"refresh_token": "secret-value"}')
    assert b"secret-value" not in path.read_bytes()
    assert gmail.load_token(path) == {"refresh_token": "secret-value"}


def _accounts(monkeypatch, outlook=False, gmail=False):
    monkeypatch.setattr(mail.settings, "outlook_enabled", outlook)
    monkeypatch.setattr(mail.settings, "gmail_enabled", gmail)


def test_tool_disabled_calls_nothing(monkeypatch):
    _accounts(monkeypatch)
    monkeypatch.setattr(mail, "_get_client", lambda account: pytest.fail("no mail service may be called"))
    r = mail.summarize_email(None, mail.SummarizeEmailArgs(), NOW, TZ)
    assert not r.ok and "No email account is enabled" in r.message


class FakeGmail:
    def __init__(self, result):
        self.result, self.calls = result, []

    def list_messages(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def test_tool_lists_gmail_in_local_time(monkeypatch):
    fake = FakeGmail([gmail._to_email(MESSAGE)])
    _accounts(monkeypatch, gmail=True)
    monkeypatch.setattr(mail, "_get_client", lambda account: fake)
    r = mail.summarize_email(None, mail.SummarizeEmailArgs(filter="unread", limit=2), NOW, TZ)
    assert fake.calls == [(("in:inbox is:unread", 2), {})]
    assert r.ok and r.message == "Latest 1 unread emails (Gmail):\n- Ali <ali@x.com>: (no subject) (Wed 14 Oct, 9:00 AM)"
    assert r.data["emails"][0]["received_local"] == "2026-10-14T09:00:00+05:00"
    assert r.data["account"] == "gmail"


def test_tool_prefers_outlook_and_respects_named_account(monkeypatch):
    fake = FakeGmail([])
    used = []
    _accounts(monkeypatch, outlook=True, gmail=True)
    monkeypatch.setattr(mail, "_get_client", lambda account: used.append(account) or fake)
    r = mail.summarize_email(None, mail.SummarizeEmailArgs(filter="recent", limit=3), NOW, TZ)
    assert used == ["outlook"] and fake.calls == [((), {"unread_only": False, "limit": 3})]
    assert r.message == "You have no emails in your inbox (Outlook)."
    mail.summarize_email(None, mail.SummarizeEmailArgs(account="gmail"), NOW, TZ)
    assert used == ["outlook", "gmail"]


def test_tool_named_account_must_be_enabled(monkeypatch):
    _accounts(monkeypatch, outlook=True)
    monkeypatch.setattr(mail, "_get_client", lambda account: pytest.fail("disabled account may not be called"))
    r = mail.summarize_email(None, mail.SummarizeEmailArgs(account="gmail"), NOW, TZ)
    assert not r.ok and r.message.startswith("Gmail isn't enabled")


@pytest.mark.parametrize("error", [gmail.GmailNotConnected("Gmail isn't connected yet."),
                                   outlook.OutlookNotConnected("Gmail isn't connected yet.")])
def test_tool_reports_mail_errors(monkeypatch, error):
    _accounts(monkeypatch, outlook=True, gmail=True)
    monkeypatch.setattr(mail, "_get_client", lambda account: FakeGmail(error))
    r = mail.summarize_email(None, mail.SummarizeEmailArgs(), NOW, TZ)
    assert not r.ok and r.message == "Gmail isn't connected yet."
