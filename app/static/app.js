// DAZIA chat UI. Plain JS, no framework.
// Security: all user/model text is inserted with textContent, never innerHTML.

const log = document.getElementById("log");
const form = document.getElementById("form");
const input = document.getElementById("input");
const sendBtn = document.getElementById("send");
const thinking = document.getElementById("thinking");
const banner = document.getElementById("banner");
const modelLabel = document.getElementById("model");
const remList = document.getElementById("reminders");
const remEmpty = document.getElementById("rem-empty");
const notifList = document.getElementById("notifications");
const alertsBtn = document.getElementById("enable-alerts");

const UNREACHABLE = "Can't reach DAZIA on the laptop. Is the server running and are you on the same network?";

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

function setBusy(busy) {
  input.disabled = busy;
  sendBtn.disabled = busy;
  thinking.hidden = !busy;
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

async function checkHealth() {
  try {
    const res = await fetch("/health");
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
  } catch (_) {
    showBanner(UNREACHABLE);
  }
}

async function loadReminders() {
  try {
    const res = await fetch("/reminders?status=pending");
    if (!res.ok) return; // keep the old list; chat errors are shown elsewhere
    const items = await res.json();
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
  } catch (_) {
    /* unreachable: the banner/chat already explains it */
  }
}

async function send(message) {
  addMessage("user", message);
  setBusy(true);
  try {
    let res;
    try {
      res = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: message }),
      });
    } catch (_) {
      addMessage("error", UNREACHABLE);
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

const POLL_MS = 15000;
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
    await fetch("/notifications/" + encodeURIComponent(id) + "/read", { method: "POST" });
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
    const res = await fetch("/notifications?unread_only=true&limit=20");
    if (!res.ok) return;
    items = await res.json();
  } catch (_) {
    return; // unreachable: the banner/chat already explains it
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

updateAlertsButton();
checkHealth();
loadReminders();
loadNotifications();
setInterval(loadNotifications, POLL_MS);
