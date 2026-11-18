const $ = (id) => document.getElementById(id);
const feed = $("feed");
const LABELS = { true: "True", false: "False", misleading: "Misleading", unverified: "Unverified" };
const pending = new Map(); // claim text -> "Checking…" card

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text) n.textContent = text;
  return n;
}

function render(event) {
  switch (event.type) {
    case "status":
      $("status").textContent = event.status === "listening" ? "Listening and fact-checking…" : "Connected, starting transcription…";
      $("stop").hidden = false;
      break;
    case "partial":
      $("caption").classList.remove("muted");
      $("caption").textContent = (event.speaker ? `${event.speaker}: ` : "") + event.text;
      break;
    case "checking":
      // A claim was just detected mid-speech; its web check is in flight.
      for (const claim of event.claims) {
        const card = el("div", "card pending");
        card.append(el("span", "badge", "Checking the web…"), el("p", "claim", claim));
        pending.set(claim, card);
        feed.append(card);
      }
      break;
    case "verdict": {
      const card = el("div", `card ${event.verdict}`);
      const pct = event.probability == null ? "" : ` · ${Math.round(event.probability * 100)}% likely true`;
      card.append(el("span", "badge", LABELS[event.verdict] + pct), el("p", "claim", event.claim));
      if (event.correction) card.append(el("p", "fix", `Right answer: ${event.correction}`));
      const meta = el("div", "meta");
      if (event.url) {
        const a = el("a", null, event.source || new URL(event.url).hostname);
        a.href = event.url;
        a.target = "_blank";
        meta.append(a, " · ");
      }
      meta.append(`${(event.latency_ms / 1000).toFixed(1)}s after it was said`);
      if (event.speaker) meta.append(` · speaker ${event.speaker}`);
      card.append(meta);
      const placeholder = pending.get(event.claim);
      if (placeholder) {
        placeholder.replaceWith(card);
        pending.delete(event.claim);
      } else {
        feed.append(card);
      }
      break;
    }
    case "error":
      feed.append(el("div", "error", event.message));
      break;
    case "closed":
      $("status").textContent = "Stopped. Click the toolbar icon to start again.";
      $("stop").hidden = true;
      break;
  }
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.target === "background" && msg.type === "event") render(msg.event);
});

$("stop").onclick = () => chrome.runtime.sendMessage({ target: "background", type: "stop" });

// Restore history and settings.
const { events = [], active } = await chrome.storage.session.get(["events", "active"]);
events.forEach(render);
if (active) { $("stop").hidden = false; $("status").textContent = "Listening and fact-checking…"; }

const s = await chrome.storage.local.get({ server: "ws://127.0.0.1:8765/stream", speak: "all", rate: 1.15 });
$("speak").value = s.speak;
$("rate").value = s.rate;
$("server").value = s.server;
$("speak").onchange = (e) => chrome.storage.local.set({ speak: e.target.value });
$("rate").onchange = (e) => chrome.storage.local.set({ rate: Number(e.target.value) });
$("server").onchange = (e) => chrome.storage.local.set({ server: e.target.value.trim() });
