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

## Test
```powershell
.venv\Scripts\python -m pytest -q
```

## Data & backup
Reminders live in `dazia.db` (SQLite, path set by `DAZIA_DB_PATH`). Back up by copying the file while the server is stopped; delete it to wipe all data.

## Troubleshooting
- Banner "model can't load": start Ollama and run `ollama pull qwen3.5:4b`.
- Slow replies: close heavy apps; the first request after start loads the model (~20 s).
