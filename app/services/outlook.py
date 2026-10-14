"""Outlook.com access through Microsoft Graph for personal accounts.

Reading uses delegated Mail.Read. Sending (Phase 12) uses Mail.Send and is only called by the
orchestrator after the user explicitly confirms a draft; the LLM has no way to send.

One-time sign-in on the laptop (opens the browser), then one test read:
    .venv/Scripts/python -m app.services.outlook
MSAL's token cache is stored encrypted with Windows DPAPI. Email content is never logged or stored;
callers only receive sender, subject, time and Graph's short preview.
"""
import logging
from datetime import datetime
from pathlib import Path

import httpx

from app.services import dpapi
from app.services.gmail import MAX_FROM_CHARS, MAX_SNIPPET_CHARS, MAX_SUBJECT_CHARS, Email, clean_text

log = logging.getLogger(__name__)

AUTHORITY = "https://login.microsoftonline.com/consumers"  # personal Microsoft accounts only
SCOPES = ["Mail.Read"]  # MSAL adds openid/profile/offline_access itself
SEND_SCOPES = ["Mail.Send"]
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
SELECT = "id,from,replyTo,subject,receivedDateTime,bodyPreview,isRead"
SIGN_IN_HINT = "Run on the laptop: python -m app.services.outlook"


class OutlookError(Exception):
    """Outlook could not be read (network, permission, API error). Message is user-facing."""


class OutlookNotConnected(OutlookError):
    """No usable sign-in: never signed in, token revoked, or expired."""


class OutlookClient:
    def __init__(self, client_id: str, token_path: str, timeout_s: float = 15.0,
                 transport: httpx.BaseTransport | None = None, app=None):
        """`transport` and `app` (an MSAL application) are only meant for tests."""
        self.client_id = client_id
        self.token_path = Path(token_path)
        self._app = app
        self._cache = None
        self._http = httpx.Client(base_url=GRAPH_BASE, timeout=timeout_s, transport=transport)

    def list_messages(self, unread_only: bool, limit: int) -> list[Email]:
        """Newest inbox messages, metadata and preview only."""
        params = {"$top": limit, "$select": SELECT, "$orderby": "receivedDateTime desc"}
        if unread_only:
            # Graph requires the $orderby property to appear first in $filter.
            params["$filter"] = "receivedDateTime ge 1900-01-01T00:00:00Z and isRead eq false"
        body = self._get("/me/mailFolders/inbox/messages", params)
        return [_to_email(m) for m in body.get("value", [])[:limit]]

    def send_mail(self, to: str, subject: str, body: str) -> None:
        """Send a new plain-text email to one recipient. Call only after the user confirmed it."""
        message = {
            "subject": subject,
            "body": {"contentType": "Text", "content": body},
            "toRecipients": [{"emailAddress": {"address": to}}],
        }
        self._post("/me/sendMail", {"message": message})

    def reply(self, message_id: str, body: str) -> None:
        """Reply to one message (Graph sends it to the message's replyTo, else its sender)."""
        self._post(f"/me/messages/{message_id}/reply", {"comment": body})

    def _post(self, path: str, payload: dict) -> None:
        headers = {"Authorization": f"Bearer {self._token(SEND_SCOPES)}"}
        try:
            resp = self._http.post(path, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            # The request may or may not have reached Microsoft: say so instead of claiming either way.
            raise OutlookError(f"I couldn't confirm the email was sent ({exc.__class__.__name__}). "
                               "Check your Sent Items before trying again.") from exc
        if resp.status_code == 202:
            return
        if resp.status_code == 403:
            raise OutlookError("Outlook refused to send (the Mail.Send permission was not granted).")
        if resp.status_code == 404:
            raise OutlookError("That email no longer exists in your mailbox. Nothing was sent.")
        _raise_for(resp)

    def _get(self, path: str, params: dict) -> dict:
        headers = {"Authorization": f"Bearer {self._token()}"}
        try:
            resp = self._http.get(path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise OutlookError(f"I couldn't reach Outlook ({exc.__class__.__name__}).") from exc
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 403:
            raise OutlookError("Outlook refused access (the Mail.Read permission was not granted).")
        if resp.status_code == 404:
            raise OutlookError("This Microsoft account has no Outlook.com mailbox that Graph can read.")
        _raise_for(resp)

    # ---- MSAL token handling ------------------------------------------------------
    def _msal(self):
        if self._app is None:
            import msal

            self._cache = msal.SerializableTokenCache()
            if self.token_path.exists():
                try:
                    self._cache.deserialize(dpapi.unprotect(self.token_path.read_bytes()).decode("utf-8"))
                except dpapi.DPAPIError as exc:
                    raise OutlookNotConnected(f"The saved Outlook sign-in could not be decrypted. {SIGN_IN_HINT}") from exc
            self._app = msal.PublicClientApplication(self.client_id, authority=AUTHORITY, token_cache=self._cache)
        return self._app

    def _save_cache(self) -> None:
        if self._cache is not None and self._cache.has_state_changed:
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_bytes(dpapi.protect(self._cache.serialize().encode("utf-8")))
            self._cache.has_state_changed = False

    def _token(self, scopes: list[str] = SCOPES) -> str:
        """Cached access token, refreshed silently with the stored refresh token when needed."""
        if not self.client_id:
            raise OutlookNotConnected("Outlook isn't set up: DAZIA_OUTLOOK_CLIENT_ID is empty.")
        app = self._msal()
        accounts = app.get_accounts()
        if not accounts:
            raise OutlookNotConnected(f"Outlook isn't connected yet. {SIGN_IN_HINT}")
        try:
            result = app.acquire_token_silent(scopes, account=accounts[0])
        except Exception as exc:  # network errors from the token endpoint
            raise OutlookError(f"I couldn't refresh the Outlook sign-in ({exc.__class__.__name__}).") from exc
        self._save_cache()
        if not result or "access_token" not in result:
            if scopes == SEND_SCOPES:
                raise OutlookNotConnected("Sending needs the Mail.Send permission. Add it to the app in Entra, "
                                          f"set DAZIA_OUTLOOK_SEND_ENABLED=true, then sign in again. {SIGN_IN_HINT}")
            raise OutlookNotConnected(f"Outlook sign-in has expired or was revoked. {SIGN_IN_HINT}")
        return result["access_token"]

    def sign_in(self, with_send: bool = False) -> None:
        """Interactive consent in the laptop's browser (loopback redirect, PKCE handled by MSAL)."""
        if not self.client_id:
            raise SystemExit("Set DAZIA_OUTLOOK_CLIENT_ID in .env first (the app's Application (client) ID).")
        scopes = SCOPES + SEND_SCOPES if with_send else SCOPES
        result = self._msal().acquire_token_interactive(scopes, prompt="select_account")
        if "access_token" not in result:
            # Error codes/descriptions from Microsoft contain no tokens.
            raise SystemExit(f"Sign-in failed: {result.get('error')}: {result.get('error_description', '')[:300]}")
        self._save_cache()


def _raise_for(resp: httpx.Response) -> None:
    """Errors shared by reads and sends."""
    if resp.status_code == 401:
        raise OutlookNotConnected(f"Outlook rejected the sign-in. {SIGN_IN_HINT}")
    if resp.status_code == 429:
        raise OutlookError("Outlook is busy (rate limited). Try again in a minute.")
    raise OutlookError(f"Outlook returned HTTP {resp.status_code}.")


def _to_email(msg: dict) -> Email:
    sender = (msg.get("from") or {}).get("emailAddress") or {}
    name, address = sender.get("name", ""), sender.get("address", "")
    sender_text = f"{name} <{address}>" if name and address and name != address else (name or address)
    received = datetime.fromisoformat(msg.get("receivedDateTime", "1970-01-01T00:00:00Z").replace("Z", "+00:00"))
    return Email(
        sender=clean_text(sender_text, MAX_FROM_CHARS) or "(unknown sender)",
        subject=clean_text(msg.get("subject") or "", MAX_SUBJECT_CHARS) or "(no subject)",
        received=received,
        snippet=clean_text(msg.get("bodyPreview") or "", MAX_SNIPPET_CHARS),
        unread=not msg.get("isRead", True),
        id=msg.get("id", ""),
        reply_to=_reply_to(msg, address),
    )


def _reply_to(msg: dict, sender_address: str) -> str:
    """The first replyTo address when it differs from the sender (a reply goes there instead)."""
    for item in msg.get("replyTo") or []:
        address = ((item or {}).get("emailAddress") or {}).get("address", "")
        if address and address.lower() != sender_address.lower():
            return clean_text(address, MAX_FROM_CHARS)
    return ""


if __name__ == "__main__":
    from app.config import settings

    client = OutlookClient(settings.outlook_client_id, settings.outlook_token_file, settings.outlook_timeout_s)
    client.sign_in(with_send=settings.outlook_send_enabled)
    access = "read + send after confirmation" if settings.outlook_send_enabled else "read-only"
    print(f"Outlook connected ({access}). Token saved encrypted to {settings.outlook_token_file}.")
    try:
        emails = client.list_messages(unread_only=False, limit=3)
    except OutlookError as exc:
        raise SystemExit(f"Sign-in worked, but the test read failed: {exc}")
    print(f"Test read OK: {len(emails)} recent inbox message(s) visible. (Content not shown.)")
