// DAZIA chat UI. Plain JS, no framework.
// Security: all user/model text is inserted with textContent, never innerHTML.
// The passphrase is never stored; the server keeps the session in an HttpOnly cookie.

const log = document.getElementById("log");
const form = document.getElementById("form");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const thinking = document.getElementById("thinking");
const banner = document.getElementById("banner");
const offlineBanner = document.getElementById("offline");
const modelLabel = document.getElementById("model");
const remList = document.getElementById("reminders");
const remEmpty = document.getElementById("rem-empty");
const remCount = document.getElementById("rem-count");
const side = document.getElementById("side");
const notifList = document.getElementById("notifications");
const alertsBtn = document.getElementById("enable-alerts");
const appView = document.getElementById("app");
const loginView = document.getElementById("login");
const loginForm = document.getElementById("login-form");
const passInput = document.getElementById("passphrase");
const loginBtn = document.getElementById("login-btn");
const loginError = document.getElementById("login-error");
const logoutBtn = document.getElementById("logout");
const micBtn = document.getElementById("mic");
const micTime = document.getElementById("mic-time");
const speakBtn = document.getElementById("speak-toggle");
const voiceStatus = document.getElementById("voice-status");
const voiceHint = document.getElementById("voice-hint");

const POLL_MS = 15000;     // notifications poll and offline retry interval
const NOT_SENT = "Message not sent — DAZIA on the laptop can't be reached.";

let authRequired = false;  // from /auth/status
let started = false;       // chat view is active and polling
let booted = false;        // /auth/status has answered at least once
let pollTimer = null;
let retryTimer = null;

// ---- Central fetch helper ---------------------------------------------------------
// Every API call goes through here so offline and 401 handling live in one place.
// Throws an Error with .kind = "offline" | "unauthorized"; otherwise returns the Response.

function apiError(kind) {
  const err = new Error(kind);
  err.kind = kind;
  return err;
}

async function api(path, options) {
  let res;
  try {
    res = await fetch(path, Object.assign({ credentials: "same-origin" }, options));
  } catch (_) {
    setOffline(true);
    throw apiError("offline");
  }
  setOffline(false);
  if (res.status === 401 && path !== "/login") {
    sessionExpired();
    throw apiError("unauthorized");
  }
  return res;
}

function postJson(path, data) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data || {}),
  });
}

// Send a Blob as the raw body with its own type (e.g. "audio/webm;codecs=opus").
function postBlob(path, blob) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": blob.type },
    body: blob,
  });
}

// ---- Offline banner + retry ---------------------------------------------------------

function setOffline(offline) {
  const wasOffline = !offlineBanner.hidden;
  offlineBanner.hidden = !offline;
  if (offline && !retryTimer) {
    retryTimer = setInterval(retry, POLL_MS);
  } else if (!offline && retryTimer) {
    clearInterval(retryTimer);
    retryTimer = null;
  }
  if (wasOffline && !offline && started) {
    loadReminders();
    loadNotifications();
  }
}

function retry() {
  if (!booted) boot();
  else checkHealth();
}

// ---- Small UI helpers ----------------------------------------------------------------

// Add one message bubble to the chat log. kind: "user" | "bot" | "clarify" | "error"
function addMessage(kind, text, meta) {
  const li = document.createElement("li");
  li.className = "msg " + kind;
  const body = document.createElement("div");
  body.className = "text";
  body.textContent = text;
  li.appendChild(body);
  if (meta) {
    const small = document.createElement("div");
    small.className = "meta";
    small.textContent = meta;
    li.appendChild(small);
  }
  log.appendChild(li);
  li.scrollIntoView({ block: "end", behavior: "smooth" });
}

// Show an error bubble, but not the same one twice in a row.
function addErrorOnce(text) {
  const last = log.lastElementChild;
  if (last && last.classList.contains("error") && last.textContent === text) return;
  addMessage("error", text);
}

function setBusy(busy) {
  input.disabled = busy;
  sendBtn.disabled = busy;
  thinking.hidden = !busy;
  updateMic();
  if (!busy) input.focus();
}

function showBanner(text) {
  banner.textContent = text;
  banner.hidden = !text;
}

// Turn a FastAPI error response into a short readable message.
async function errorText(res) {
  try {
    const body = await res.json();
    if (Array.isArray(body.detail)) {
      return body.detail.map((d) => d.msg).join("; ");
    }
    if (body.detail) return String(body.detail);
  } catch (_) { /* not JSON */ }
  return "Server error (" + res.status + ").";
}

function formatDue(iso, tz) {
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  try {
    return d.toLocaleString(undefined, { timeZone: tz, dateStyle: "medium", timeStyle: "short" });
  } catch (_) {
    return d.toLocaleString(); // unknown tz name in this browser
  }
}

// ---- Login / logout ------------------------------------------------------------------

function showLogin(message) {
  stopApp();
  appView.hidden = true;
  loginView.hidden = false;
  loginError.textContent = message || "";
  loginBtn.disabled = false;
  passInput.value = "";
  passInput.focus();
}

// Any API call got 401: the session expired or the cookie is gone.
function sessionExpired() {
  if (!started) return; // already on the login screen (or logging out)
  authRequired = true;
  logoutBtn.hidden = false;
  showLogin("Please log in again.");
}

loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const passphrase = passInput.value;
  if (!passphrase || loginBtn.disabled) return;
  loginBtn.disabled = true;
  loginError.textContent = "";
  try {
    const res = await postJson("/login", { passphrase: passphrase });
    if (res.ok) {
      passInput.value = "";
      startApp();
      return;
    }
    loginError.textContent = res.status === 401 ? "Wrong passphrase." : await errorText(res);
  } catch (_) {
    loginError.textContent = "Can't reach DAZIA right now.";
  } finally {
    loginBtn.disabled = false;
  }
});

logoutBtn.addEventListener("click", async () => {
  try {
    await postJson("/logout");
  } catch (_) {
    return; // offline: the banner explains it; stay logged in
  }
  started = false; // ignore 401s from requests still in flight
  log.replaceChildren();
  notifList.replaceChildren();
  remList.replaceChildren();
  showLogin("");
});

// ---- Health, reminders, chat -------------------------------------------------------

async function checkHealth() {
  let res;
  try {
    res = await api("/health");
  } catch (_) {
    return; // offline banner or login screen already shown
  }
  if (!res.ok) {
    showBanner("DAZIA health check failed: " + (await errorText(res)));
    return;
  }
  const h = await res.json();
  modelLabel.textContent = h.model || "";
  if (h.llm_ok) {
    showBanner("");
  } else {
    showBanner("The local AI model (" + h.model + ") can't be loaded: " + h.llm_detail +
      " — check that Ollama is running and the model is pulled.");
  }
}

async function loadReminders() {
  let items;
  try {
    const res = await api("/reminders?status=pending");
    if (!res.ok) return; // keep the old list; chat errors are shown elsewhere
    items = await res.json();
  } catch (_) {
    return; // offline banner or login screen already shown
  }
  remList.replaceChildren();
  for (const r of items) {
    const li = document.createElement("li");
    const title = document.createElement("span");
    title.className = "rem-title";
    title.textContent = r.title;
    const due = document.createElement("span");
    due.className = "rem-due";
    due.textContent = formatDue(r.due_utc, r.tz);
    li.append(title, due);
    remList.appendChild(li);
  }
  remEmpty.hidden = items.length > 0;
  remCount.textContent = items.length ? "(" + items.length + ")" : "";
}

async function send(message) {
  addMessage("user", message);
  setBusy(true);
  try {
    let res;
    try {
      res = await postJson("/chat", { message: message });
    } catch (err) {
      if (!input.value) input.value = message; // let the user resend it
      if (err.kind === "offline") addErrorOnce(NOT_SENT);
      return;
    }
    if (!res.ok) {
      addMessage("error", await errorText(res));
      return;
    }
    const data = await res.json();
    if (data.needs_clarification) {
      addMessage("clarify", data.reply, "DAZIA needs more detail");
    } else {
      addMessage("bot", data.reply, data.tool ? "tool: " + data.tool : "");
    }
    speakReply(data.reply);
    loadReminders();
    loadNotifications();
  } finally {
    setBusy(false);
  }
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const message = input.value.trim();
  if (!message || sendBtn.disabled) return;
  input.value = "";
  if (micState !== "recording" && micState !== "transcribing") setVoiceStatus("");
  send(message);
});

// Enter sends; Shift+Enter makes a new line.
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    form.requestSubmit();
  }
});

// ---- Notifications feed -------------------------------------------------------
// The server writes a notification when a reminder fires. We poll for unread ones,
// show them above the chat, and mark them read when dismissed.

const seenIds = new Set();      // ids already shown at least once (no repeat browser alerts)
const dismissedIds = new Set(); // dismissed here; hide even if a slow poll still returns them
let firstLoad = true;

const browserAlertsSupported = "Notification" in window;

function updateAlertsButton() {
  alertsBtn.hidden = !browserAlertsSupported || Notification.permission !== "default";
}

// Permission may only be requested from a user click.
alertsBtn.addEventListener("click", async () => {
  try {
    await Notification.requestPermission();
  } catch (_) { /* old browsers or blocked: just hide the button */ }
  updateAlertsButton();
});

function showBrowserAlert(n) {
  if (!browserAlertsSupported || Notification.permission !== "granted") return;
  try {
    new Notification(n.title, { body: n.body, tag: "dazia-" + n.id });
  } catch (_) { /* e.g. mobile browsers that need a service worker: skip */ }
}

async function dismissNotification(id, li) {
  dismissedIds.add(id);
  li.remove();
  try {
    await api("/notifications/" + encodeURIComponent(id) + "/read", { method: "POST" });
  } catch (_) { /* offline: it shows again after reload, which is fine */ }
}

function renderNotification(n) {
  const li = document.createElement("li");
  li.className = "notification " + (n.kind === "missed" ? "missed" : "due");
  const text = document.createElement("div");
  text.className = "n-text";
  const title = document.createElement("div");
  title.className = "n-title";
  title.textContent = n.title;
  const body = document.createElement("div");
  body.textContent = n.body;
  text.append(title, body);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.textContent = "Dismiss";
  btn.setAttribute("aria-label", "Dismiss notification");
  btn.addEventListener("click", () => dismissNotification(n.id, li));
  li.append(text, btn);
  return li;
}

async function loadNotifications() {
  let items;
  try {
    const res = await api("/notifications?unread_only=true&limit=20");
    if (!res.ok) return;
    items = await res.json();
  } catch (_) {
    return; // offline banner or login screen already shown
  }
  let hasNew = false;
  notifList.replaceChildren();
  for (const n of items) {
    if (dismissedIds.has(n.id)) continue;
    if (!seenIds.has(n.id)) {
      seenIds.add(n.id);
      hasNew = true;
      if (!firstLoad) showBrowserAlert(n); // don't alert old items on page load
    }
    notifList.appendChild(renderNotification(n));
  }
  firstLoad = false;
  if (hasNew) loadReminders(); // a fired reminder may have changed the pending list
}

// ---- Voice input (push-to-talk) --------------------------------------------------
// Tap the mic to start, tap again to stop. The recording is transcribed on the laptop
// and the text goes INTO the input box so the user can fix it before sending.

const REC_TYPES = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/mp4"];
const REC_MAX_MS = 29500; // server limit is 30s; stop a little early so encoding overhead fits
const SPEAK_KEY = "dazia.speakReplies";

let micState = "idle";   // "idle" | "starting" | "recording" | "transcribing"
let recorder = null;
let recDiscard = false;  // true when a recording is cancelled (logout) instead of transcribed
let recTimer = null;

function setVoiceStatus(text) {
  voiceStatus.textContent = text || "";
}

function updateMic() {
  const recording = micState === "recording";
  micBtn.classList.toggle("recording", recording);
  micBtn.setAttribute("aria-label", recording ? "Stop recording" : "Start voice input");
  micBtn.disabled = micState === "starting" || micState === "transcribing" ||
    (micState === "idle" && sendBtn.disabled);
  if (!recording) micTime.textContent = "";
}

function pickRecType() {
  for (const t of REC_TYPES) {
    if (MediaRecorder.isTypeSupported(t)) return t;
  }
  return ""; // let the browser choose
}

function micErrorText(err) {
  const name = err && err.name;
  if (name === "NotAllowedError" || name === "SecurityError") {
    return "Microphone access was blocked. Allow the microphone for this site " +
      "(tap the icon next to the address bar), then try again.";
  }
  if (name === "NotFoundError") return "No microphone was found on this device.";
  if (name === "NotReadableError") return "The microphone is busy in another app.";
  return "Couldn't start the microphone.";
}

async function startRecording() {
  micState = "starting";
  updateMic();
  setVoiceStatus("");
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    micState = "idle";
    updateMic();
    setVoiceStatus(micErrorText(err));
    return;
  }
  const stopTracks = () => stream.getTracks().forEach((t) => t.stop()); // mic indicator off
  const type = pickRecType();
  let rec;
  try {
    rec = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
  } catch (_) {
    stopTracks();
    micState = "idle";
    updateMic();
    setVoiceStatus("This browser can't record audio.");
    return;
  }
  const chunks = [];
  rec.addEventListener("dataavailable", (e) => {
    if (e.data && e.data.size) chunks.push(e.data);
  });
  rec.addEventListener("stop", () => {
    stopTracks();
    clearInterval(recTimer);
    recTimer = null;
    recorder = null;
    micState = "idle";
    updateMic();
    if (recDiscard || !started) {
      setVoiceStatus("");
      return;
    }
    const blobType = (chunks[0] && chunks[0].type) || rec.mimeType || type || "audio/webm";
    transcribe(new Blob(chunks, { type: blobType }));
  });

  recorder = rec;
  recDiscard = false;
  rec.start();
  micState = "recording";
  updateMic();
  setVoiceStatus("Recording… tap the mic again to stop.");
  const startedAt = Date.now();
  micTime.textContent = "0s";
  recTimer = setInterval(() => {
    const ms = Date.now() - startedAt;
    micTime.textContent = Math.floor(ms / 1000) + "s";
    if (ms >= REC_MAX_MS) stopRecording();
  }, 250);
}

function stopRecording(discard) {
  if (!recorder || recorder.state === "inactive") return;
  recDiscard = Boolean(discard);
  recorder.stop(); // the "stop" handler does the rest
}

async function transcribe(blob) {
  if (!blob.size) {
    setVoiceStatus("I didn't catch that — try again.");
    return;
  }
  micState = "transcribing";
  updateMic();
  setVoiceStatus("Transcribing…");
  try {
    let res;
    try {
      res = await postBlob("/transcribe", blob);
    } catch (err) {
      setVoiceStatus(err.kind === "offline" ? "Can't reach DAZIA — the recording wasn't transcribed." : "");
      return;
    }
    if (!res.ok) {
      setVoiceStatus(await errorText(res));
      return;
    }
    const data = await res.json();
    const text = String(data.text || "").trim();
    if (!text) {
      setVoiceStatus("I didn't catch that — try again.");
      return;
    }
    setVoiceStatus("Check the text, then send.");
    const before = input.value.trim();
    input.value = before ? before + " " + text : text;
  } finally {
    micState = "idle";
    updateMic();
    if (!input.disabled) {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  }
}

micBtn.addEventListener("click", () => {
  if (micState === "idle") startRecording();
  else if (micState === "recording") stopRecording();
});

// ---- Voice output ("Speak replies") -------------------------------------------------

let speakOn = false;
let speakToken = 0;      // bumps on every new reply, so a slow /speak can't talk over a newer one
let currentAudio = null;

function readSpeakPref() {
  try {
    return localStorage.getItem(SPEAK_KEY) === "1";
  } catch (_) {
    return false;
  }
}

function setSpeakOn(on) {
  speakOn = on;
  speakBtn.setAttribute("aria-pressed", on ? "true" : "false");
  try {
    localStorage.setItem(SPEAK_KEY, on ? "1" : "0");
  } catch (_) { /* private mode: just don't remember it */ }
  if (!on) stopSpeaking();
}

function stopSpeaking() {
  speakToken++;
  if (currentAudio) {
    currentAudio.pause();
    URL.revokeObjectURL(currentAudio.src);
    currentAudio = null;
  }
  if ("speechSynthesis" in window) window.speechSynthesis.cancel();
}

function browserSpeak(text, why) {
  if (!("speechSynthesis" in window) || typeof SpeechSynthesisUtterance === "undefined") {
    console.warn("DAZIA: can't speak reply:", why);
    return;
  }
  window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
}

async function speakReply(text) {
  if (!speakOn || speakBtn.hidden || !text) return;
  stopSpeaking();
  const token = speakToken;
  let url = null;
  try {
    const res = await postJson("/speak", { text: text });
    if (!res.ok) throw new Error("/speak returned " + res.status);
    const blob = await res.blob();
    if (token !== speakToken) return; // stopped or replaced meanwhile
    url = URL.createObjectURL(blob);
    const audio = new Audio(url);
    currentAudio = audio;
    const done = () => {
      URL.revokeObjectURL(url);
      if (currentAudio === audio) currentAudio = null;
    };
    audio.addEventListener("ended", done);
    audio.addEventListener("error", done);
    await audio.play();
  } catch (err) {
    if (err.kind === "unauthorized" || token !== speakToken) return;
    if (url) URL.revokeObjectURL(url);
    currentAudio = null;
    browserSpeak(text, err);
  }
}

speakBtn.addEventListener("click", () => setSpeakOn(!speakOn));

// Show the voice controls the server and this browser support.
async function loadVoiceStatus() {
  let v;
  try {
    const res = await api("/voice/status");
    if (!res.ok) return; // older server or voice routes missing: keep voice hidden
    v = await res.json();
  } catch (_) {
    return;
  }
  const canRecord = Boolean(navigator.mediaDevices && navigator.mediaDevices.getUserMedia) &&
    "MediaRecorder" in window && window.isSecureContext;
  micBtn.hidden = !(v.stt_enabled && canRecord);
  voiceHint.hidden = !(v.stt_enabled && !window.isSecureContext);
  speakBtn.hidden = !v.tts_enabled;
  speakOn = readSpeakPref();
  speakBtn.setAttribute("aria-pressed", speakOn ? "true" : "false");
  updateMic();
}

function stopVoice() {
  stopRecording(true);
  stopSpeaking();
  setVoiceStatus("");
}

// ---- Startup ---------------------------------------------------------------------

function startApp() {
  loginView.hidden = true;
  appView.hidden = false;
  started = true;
  updateAlertsButton();
  checkHealth();
  loadReminders();
  loadNotifications();
  loadVoiceStatus();
  if (!pollTimer) pollTimer = setInterval(loadNotifications, POLL_MS);
}

function stopApp() {
  started = false;
  stopVoice();
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function boot() {
  let status;
  try {
    const res = await api("/auth/status");
    if (!res.ok) {
      showBanner("DAZIA login check failed: " + (await errorText(res)));
      return;
    }
    status = await res.json();
  } catch (_) {
    return; // offline: retry() calls boot() again every 15s
  }
  booted = true;
  authRequired = Boolean(status.auth_required);
  logoutBtn.hidden = !authRequired;
  if (authRequired && !status.authenticated) showLogin("");
  else startApp();
}

// Small screens: start with the reminders panel collapsed below the chat.
if (window.matchMedia("(max-width: 799px)").matches) side.open = false;

// Service worker only works in a secure context (HTTPS or localhost).
// On plain-http LAN access this silently skips, which is expected.
if ("serviceWorker" in navigator && window.isSecureContext) {
  navigator.serviceWorker.register("/sw.js").catch(() => { /* app works without it */ });
}

boot();
