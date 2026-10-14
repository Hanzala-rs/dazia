import httpx
import pytest

from app.services import outlook

MSG = {
    "from": {"emailAddress": {"name": "Ali", "address": "ali@x.com"}},
    "subject": "Invoice  due",
    "receivedDateTime": "2026-10-14T04:00:00Z",
    "bodyPreview": "Please pay\r\ninvoice #42",
    "isRead": False,
}


class FakeMSAL:
    def __init__(self, accounts=({"username": "me@outlook.com"},), result=None):
        self.accounts = list(accounts)
        self.result = result if result is not None else {"access_token": "tok"}

    def get_accounts(self):
        return self.accounts

    def acquire_token_silent(self, scopes, account):
        assert scopes == ["Mail.Read"]
        return self.result


def make(handler, tmp_path, **msal_kwargs):
    return outlook.OutlookClient("client-id", str(tmp_path / "t.bin"),
                                 transport=httpx.MockTransport(handler), app=FakeMSAL(**msal_kwargs))


def test_unread_query_and_parsing(tmp_path):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"value": [MSG]})

    emails = make(handler, tmp_path).list_messages(unread_only=True, limit=5)
    req = seen[0]
    assert req.method == "GET" and req.url.path == "/v1.0/me/mailFolders/inbox/messages"
    assert req.url.params["$top"] == "5" and "isRead eq false" in req.url.params["$filter"]
    assert req.url.params["$select"] == outlook.SELECT  # no full body requested
    assert req.headers["Authorization"] == "Bearer tok"
    e = emails[0]
    assert (e.sender, e.subject, e.snippet, e.unread) == ("Ali <ali@x.com>", "Invoice due", "Please pay invoice #42", True)
    assert e.received.isoformat() == "2026-10-14T04:00:00+00:00"


def test_recent_has_no_filter(tmp_path):
    seen = []
    make(lambda req: seen.append(req) or httpx.Response(200, json={"value": []}), tmp_path).list_messages(False, 3)
    assert "$filter" not in seen[0].url.params


@pytest.mark.parametrize("status,error,text", [
    (401, outlook.OutlookNotConnected, "rejected"),
    (403, outlook.OutlookError, "Mail.Read"),
    (404, outlook.OutlookError, "no Outlook.com mailbox"),
    (429, outlook.OutlookError, "busy"),
])
def test_http_errors_are_reported(tmp_path, status, error, text):
    client = make(lambda req: httpx.Response(status), tmp_path)
    with pytest.raises(error, match=text):
        client.list_messages(True, 5)


def test_not_signed_in_or_expired(tmp_path):
    never = make(lambda req: pytest.fail("no request without a token"), tmp_path, accounts=())
    with pytest.raises(outlook.OutlookNotConnected, match="isn't connected"):
        never.list_messages(True, 5)
    expired = make(lambda req: pytest.fail("no request without a token"), tmp_path, result={"error": "invalid_grant"})
    with pytest.raises(outlook.OutlookNotConnected, match="expired"):
        expired.list_messages(True, 5)


def test_missing_client_id(tmp_path):
    client = outlook.OutlookClient("", str(tmp_path / "t.bin"))
    with pytest.raises(outlook.OutlookNotConnected, match="CLIENT_ID"):
        client.list_messages(True, 5)
