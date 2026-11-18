const $ = (id) => document.getElementById(id);
const feed = $("feed");
const LABELS = { true: "True", false: "False", misleading: "Misleading", unverified: "Unverified" };
let lastSentence = null;

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
    case "sentence": {
      // Placeholder while the claim is being checked; replaced by verdicts.
      lastSentence = el("div", "card pending");
      lastSentence.append(el("span", "badge", "Checking…"), el("p", "claim", event.text));
      feed.append(lastSentence);
      break;
    }
    case "verdict": {
      if (lastSentence && lastSentence.classList.contains("pending")) {
        lastSentence.remove();
        lastSentence = null;
      }
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
      meta.append(`${(event.latency_ms / 1000).toFixed(1)}s after the sentence`);
      if (event.speaker) meta.append(` · speaker ${event.speaker}`);
      card.append(meta);
      feed.append(card);
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
