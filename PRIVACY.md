# DAZIA Privacy Policy

DAZIA is a personal assistant that its owner runs on their own laptop. It is not a hosted service and has
no other users.

## Gmail data
- DAZIA requests only the read-only Gmail scope (`gmail.readonly`). It cannot send, delete, or modify email.
- Messages are fetched on request (for example "summarize my unread email"), processed on the laptop by a
  local language model, and shown to the owner. Email content is not stored in DAZIA's database and is not
  written to logs.
- The Google sign-in token is stored on the laptop, encrypted with Windows Data Protection (DPAPI).

## Outlook.com data
- DAZIA requests the delegated Microsoft Graph permission `Mail.Read` for a personal Microsoft account. It
  cannot delete or modify email.
- It reads sender, subject, received time and Graph's short preview of inbox messages on request; the full
  message body is not requested. Previews are summarized by the local model and not stored or logged.
- The Microsoft sign-in token cache is stored on the laptop, encrypted with Windows DPAPI.
- Sending is off by default. If the owner turns it on, DAZIA also requests `Mail.Send` and sends an email only
  after showing the full draft and receiving the owner's explicit "send". Draft text is kept in memory only and
  is never written to the database or logs.

## Sharing
- No email data, account data, or usage data is sent to the developer, sold, or shared with any third party.
- DAZIA uses no hosted AI services; language, speech, and summaries all run locally.
- Google API data use complies with the
  [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
  including the Limited Use requirements.

## Removing access
- Gmail: revoke DAZIA at https://myaccount.google.com/permissions and delete `secrets/gmail_token.bin`.
- Outlook: remove DAZIA at https://account.live.com/consent/Manage and delete `secrets/outlook_token.bin`.

Contact: open an issue at https://github.com/Hanzala-rs/dazia/issues
