# DAZIA Privacy Policy

DAZIA is a personal assistant that its owner runs on their own laptop. It is not a hosted service and has
no other users.

## Gmail data
- DAZIA requests only the read-only Gmail scope (`gmail.readonly`). It cannot send, delete, or modify email.
- Messages are fetched on request (for example "summarize my unread email"), processed on the laptop by a
  local language model, and shown to the owner. Email content is not stored in DAZIA's database and is not
  written to logs.
- The Google sign-in token is stored on the laptop, encrypted with Windows Data Protection (DPAPI).

## Sharing
- No email data, account data, or usage data is sent to the developer, sold, or shared with any third party.
- DAZIA uses no hosted AI services; language, speech, and summaries all run locally.
- Google API data use complies with the
  [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy),
  including the Limited Use requirements.

## Removing access
Revoke DAZIA at https://myaccount.google.com/permissions and delete the local token file
(`secrets/gmail_token.bin`).

Contact: open an issue at https://github.com/Hanzala-rs/dazia/issues
