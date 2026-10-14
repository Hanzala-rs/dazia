"""Gmail read-only access (scope gmail.readonly): sign-in, encrypted token storage, inbox fetch.

One-time sign-in on the laptop (opens the browser):  .venv/Scripts/python -m app.services.gmail
The token file is encrypted with Windows DPAPI, so only this Windows user can read it.
Email content is never logged or stored; callers only receive short previews.
"""
import ctypes
import html
import json
import logging
import sys
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_SUBJECT_CHARS = 150
MAX_FROM_CHARS = 100
MAX_SNIPPET_CHARS = 200


class GmailError(Exception):
    """Gmail could not be read (network, permission, API error). Message is user-facing."""


class GmailNotConnected(GmailError):
    """No usable sign-in: never signed in, token revoked, or expired."""


@dataclass
class Email:
    sender: str
    subject: str
    received: datetime  # aware, UTC
    snippet: str  # Gmail's short plain-text preview, not the full body
    unread: bool


# ---- token encryption (Windows DPAPI, current user) --------------------------
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise GmailError("Gmail token encryption needs Windows (DPAPI).")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    buffer = ctypes.create_string_buffer(data, len(data))
    blob_in = _Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    blob_out = _Blob()
    func = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ui_forbidden = 0x1
    if not func(ctypes.byref(blob_in), None, None, None, None, ui_forbidden, ctypes.byref(blob_out)):
        raise GmailNotConnected("The saved Gmail sign-in could not be decrypted. Sign in again.")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def save_token(path: Path, token_json: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_dpapi(token_json.encode("utf-8"), protect=True))


def load_token(path: Path) -> dict:
    if not path.exists():
        raise GmailNotConnected("Gmail isn't connected yet. On the laptop run: python -m app.services.gmail")
    return json.loads(_dpapi(path.read_bytes(), protect=False).decode("utf-8"))


# ---- API client ---------------------------------------------------------------
class GmailClient:
    def __init__(self, token_path: str, timeout_s: float = 15.0, transport: httpx.BaseTransport | None = None):
        """`transport` is optional and only meant for tests (e.g. httpx.MockTransport)."""
        self.token_path = Path(token_path)
        self._creds = None  # google.oauth2.credentials.Credentials, loaded on first use
        self._http = httpx.Client(base_url=API_BASE, timeout=timeout_s, transport=transport)

    def list_messages(self, query: str, limit: int) -> list[Email]:
        """Newest messages matching a Gmail search `query`, with metadata and a short preview only."""
        listing = self._get("/messages", {"q": query, "maxResults": limit})
        ids = [m["id"] for m in listing.get("messages", [])[:limit]]
        params = {"format": "metadata", "metadataHeaders": ["From", "Subject"]}
        return [_to_email(self._get(f"/messages/{mid}", params)) for mid in ids]

    def _get(self, path: str, params: dict) -> dict:
        for attempt in (1, 2):  # second attempt only after refreshing an expired token
            try:
                resp = self._http.get(path, params=params, headers={"Authorization": f"Bearer {self._token()}"})
            except httpx.HTTPError as exc:
                raise GmailError(f"I couldn't reach Gmail ({exc.__class__.__name__}).") from exc
            if resp.status_code == 401 and attempt == 1:
                self._refresh()
                continue
            if resp.status_code == 401:
                raise GmailNotConnected("Gmail rejected the sign-in. Run: python -m app.services.gmail")
            if resp.status_code == 403:
                raise GmailError("Gmail refused access (permission or API not enabled for this project).")
            if resp.status_code != 200:
                raise GmailError(f"Gmail returned HTTP {resp.status_code}.")
            return resp.json()
        raise GmailError("Gmail request failed.")

    def _token(self) -> str:
        if self._creds is None:
            from google.oauth2.credentials import Credentials

            self._creds = Credentials.from_authorized_user_info(load_token(self.token_path), SCOPES)
        if not self._creds.valid:
            self._refresh()
        return self._creds.token

    def _refresh(self) -> None:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request

        if self._creds is None or not self._creds.refresh_token:
            raise GmailNotConnected("Gmail sign-in has expired. Run: python -m app.services.gmail")
        try:
            self._creds.refresh(Request())
        except RefreshError as exc:
            raise GmailNotConnected("Gmail sign-in has expired or was revoked. Run: python -m app.services.gmail") from exc
        except Exception as exc:  # network errors from the token endpoint
            raise GmailError(f"I couldn't refresh the Gmail sign-in ({exc.__class__.__name__}).") from exc
        save_token(self.token_path, self._creds.to_json())
        log.info("Gmail token refreshed")


def _to_email(msg: dict) -> Email:
    headers = {h.get("name", "").lower(): h.get("value", "") for h in msg.get("payload", {}).get("headers", [])}
    received = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc)
    return Email(
        sender=_clean(headers.get("from", ""), MAX_FROM_CHARS) or "(unknown sender)",
        subject=_clean(headers.get("subject", ""), MAX_SUBJECT_CHARS) or "(no subject)",
        received=received,
        snippet=_clean(html.unescape(msg.get("snippet", "")), MAX_SNIPPET_CHARS),
        unread="UNREAD" in msg.get("labelIds", []),
    )


def _clean(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def sign_in(client_file: str, token_file: str) -> None:
    """Interactive one-time consent in the laptop's browser; stores the encrypted token."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_secrets_file(client_file, SCOPES)
    creds = flow.run_local_server(port=0)  # loopback redirect on a free port
    save_token(Path(token_file), creds.to_json())
    print(f"Gmail connected (read-only). Token saved encrypted to {token_file}.")


if __name__ == "__main__":
    from app.config import settings

    sign_in(settings.gmail_client_file, settings.gmail_token_file)
