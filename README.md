# DAZIA

Local-first personal assistant (FastAPI + Ollama + SQLite). No paid AI APIs; nothing leaves the laptop on the default path.

## Model
- Default: `qwen3.5:4b` (Q4_K_M, ~3.1 GB RAM loaded), run with thinking off.
- Benchmarked on i3-1115G4 / 12 GB: ~6 tok/s, ~11 s per request warm, ~21 s first load. `llama3.1:8b` was slower (~4.4 tok/s, 5.3 GB).
- Change with `DAZIA_OLLAMA_MODEL` in `.env`.

## Install
```powershell
ollama pull qwen3.5:4b
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env   # optional; defaults work
```

## Run
```powershell
.venv\Scripts\python -m app.main
```
Open http://127.0.0.1:8000. The server binds to localhost only.

## Phone on home Wi-Fi
1. Create a passphrase hash: `.venv\Scripts\python -m app.auth` (asks twice, min 8 characters).
2. Paste the printed `DAZIA_PASSPHRASE_HASH=...` line into `.env` and set `DAZIA_HOST=0.0.0.0`.
   DAZIA refuses to start on a non-local address without a passphrase.
3. In PowerShell **as Administrator**: `powershell -ExecutionPolicy Bypass -File scripts\allow-lan.ps1`.
   It stops if your Wi-Fi is marked Public, allows port 8000 from your local subnet only, and prints the URL.
   Undo with `-Remove`.
4. Start DAZIA, open `http://<laptop-ip>:8000` on the phone and log in.

Limitation: over plain `http` the browser treats the page as not secure, so install-as-app (service worker)
and, later, the microphone won't work until Phase 5 adds HTTPS via Tailscale. Chat, reminders and
notifications work. Only use this on a trusted home network.

## Phone anywhere (HTTPS over Tailscale)
Recommended way to use DAZIA from the phone, at home or on mobile data. Tailscale makes a private network
between your own devices; `tailscale serve` gives the laptop a real HTTPS address that only your devices can
open. DAZIA itself stays on `127.0.0.1`: no Wi-Fi binding, no firewall rule, no router port forwarding.

1. Set a login passphrase (step 1–2 of "Phone on home Wi-Fi") but keep `DAZIA_HOST=127.0.0.1`.
   DAZIA refuses remote requests until a passphrase is set.
2. Install Tailscale on the laptop (https://tailscale.com/download) and the phone (Play Store) and sign in to
   the **same** account on both.
3. In the Tailscale admin console → DNS: make sure **MagicDNS** is on and enable **HTTPS Certificates**.
4. On the laptop: `tailscale serve --bg 8000` (it prints `https://<laptop>.<tailnet>.ts.net`).
   Check with `tailscale serve status`; undo with `tailscale serve reset`.
5. Start DAZIA, open that `https://...ts.net` address on the phone and log in. Microphone and
   "install as app" work here because the page is HTTPS.

Never use `tailscale funnel` — that would publish DAZIA to the whole internet.
If you used the Wi-Fi setup before, you can remove it: `scripts\allow-lan.ps1 -Remove` and set `DAZIA_HOST=127.0.0.1`.
Remote use still needs the laptop on, awake and online. Tailscale's free personal plan is enough; check its current terms.

## Voice
Speech runs entirely on the laptop; no audio or text leaves it.
- Voice input: faster-whisper `base.en`. Downloaded automatically on first use into `models/whisper`.
- Voice output: Piper `en_US-lessac-medium`. Download it once:
  ```powershell
  .venv\Scripts\python -m piper.download_voices en_US-lessac-medium --download-dir models/piper
  ```
Both models load on first use (a few seconds), not at startup.

Settings (`.env`):
| Setting | Default | Meaning |
|---|---|---|
| `DAZIA_STT_ENABLED` | `true` | Voice input on/off |
| `DAZIA_TTS_ENABLED` | `true` | Voice output on/off |
| `DAZIA_STT_MODEL` | `base.en` | faster-whisper model name |
| `DAZIA_TTS_VOICE` | `models/piper/en_US-lessac-medium.onnx` | Piper voice file (`.onnx.json` must sit next to it) |

Limitation: browsers only allow the microphone on a secure context. It works at
http://127.0.0.1:8000 on the laptop, but on the phone over plain `http` it needs Phase 5 HTTPS (Tailscale).

## Daily briefing
Ask "brief me" or "what does my day look like?" to get today's date, overdue reminders, and the rest of
today's reminders. It is built from the local database only (no model call for the content, no internet).

To get it automatically each day, set a local time in `.env`:
```
DAZIA_BRIEFING_TIME=08:00
```
It appears in the notification feed and as a Windows toast, once per day. If the laptop is off or asleep at
that time, it arrives on the first check after DAZIA is running again that day. Leave it empty to turn it off.

## Gmail (read-only)
Ask "summarize my unread email" or "show my recent emails". DAZIA uses only the `gmail.readonly` scope:
it can't send, delete, or change mail. Previews (sender, subject, Gmail's ~200-char snippet) are fetched on request,
summarized by the local model, and not saved or logged. See [PRIVACY.md](PRIVACY.md).

Setup (once):
1. Google Cloud Console: enable the Gmail API, create an OAuth client of type **Desktop app**, add your Gmail
   address as a test user, and save the client JSON as `secrets/gmail_client.json` (git-ignored).
2. Sign in on the laptop (opens the browser):
   ```powershell
   .venv\Scripts\python -m app.services.gmail
   ```
   The token is saved to `secrets/gmail_token.bin`, encrypted with Windows DPAPI.
3. Set `DAZIA_GMAIL_ENABLED=true` in `.env` and restart DAZIA.

Limitation: while the OAuth app is in Google's "Testing" status, the sign-in expires after 7 days; DAZIA then
says so and you run step 2 again. To disconnect, revoke DAZIA at https://myaccount.google.com/permissions and
delete `secrets/gmail_token.bin`.

Status: not yet verified against a real inbox. The owner's Google account is in the Advanced Protection Program,
which blocks unverified apps from Gmail scopes (`Error 400: policy_enforced` at sign-in). Use a Gmail account
outside Advanced Protection to connect. Unit tests cover the client, tool, and summary flow with mocked Gmail.

## Test
```powershell
.venv\Scripts\python -m pytest -q
```

## Data & backup
Reminders live in `dazia.db` (SQLite, path set by `DAZIA_DB_PATH`). Back up by copying the file while the server is stopped; delete it to wipe all data.

## Troubleshooting
- Banner "model can't load": start Ollama and run `ollama pull qwen3.5:4b`.
- Slow replies: close heavy apps; the first request after start loads the model (~20 s).
