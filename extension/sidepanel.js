const $ = (id) => document.getElementById(id);
const feed = $("feed");
const LABELS = { true: "True", false: "False", misleading: "Misleading", unverified: "Unverified" };
const pending = new Map(); // claim text -> { card, timer }

// Latency meter: full bar = 4 s. Stages are colour-coded segments.
const METER_MAX_MS = 4000;
const STAGES = [
  ["detect", "Claim spotted"],
  ["decide", "Web check"],
  ["correction", "Right answer"],
];

function rating(ms) {
  if (ms < 1500) return ["fast", "Blink-fast"];
  if (ms < 3000) return ["ok", "Quick"];
  return ["slow", "Slow"];
}

function seconds(ms) {
  return `${(ms / 1000).toFixed(2)}s`;
}

// Final meter: stacked bar showing where the time went.
function meter(totalMs, timings = {}) {
  const [cls, label] = rating(totalMs);
  const wrap = el("div", `meter ${cls}`);
  const head = el("div", "meter-head");
  head.append(el("span", "meter-total", seconds(totalMs)), el("span", "meter-rating", label));
  const bar = el("div", "meter-bar");
  bar.setAttribute("role", "img");
  bar.setAttribute("aria-label", `Latency ${seconds(totalMs)}`);
  const legend = el("div", "meter-legend");
  let accounted = 0;
  for (const [key, name] of STAGES) {
    const ms = timings[key];
    if (!ms) continue;
    accounted += ms;
    const seg = el("span", `seg seg-${key}`);
    seg.style.width = `${Math.min(100, (ms / METER_MAX_MS) * 100)}%`;
    seg.title = `${name}: ${seconds(ms)}`;
    bar.append(seg);
    const item = el("span", "legend-item");
    item.append(el("i", `dot seg-${key}`), `${name} ${seconds(ms)}`);
    legend.append(item);
  }
  const other = totalMs - accounted;
  if (other > 30) {
    const seg = el("span", "seg seg-other");
    seg.style.width = `${Math.min(100, (other / METER_MAX_MS) * 100)}%`;
    seg.title = `Other: ${seconds(other)}`;
    bar.append(seg);
  }
  wrap.append(head, bar, legend);
  return wrap;
}

// Live meter on a pending card: counts up until the verdict arrives.
function liveMeter(startMs) {
  const wrap = el("div", "meter live");
  const head = el("div", "meter-head");
  const total = el("span", "meter-total", seconds(startMs));
  head.append(total, el("span", "meter-rating", "Checking the web…"));
  const bar = el("div", "meter-bar");
  const fill = el("span", "seg seg-live");
  bar.append(fill);
  wrap.append(head, bar);
  const t0 = performance.now() - startMs;
  const timer = setInterval(() => {
    const ms = performance.now() - t0;
    total.textContent = seconds(ms);
    fill.style.width = `${Math.min(100, (ms / METER_MAX_MS) * 100)}%`;
    if (ms > 15000) clearInterval(timer); // give up animating
  }, 50);
  return { wrap, timer };
}

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
        const live = liveMeter(event.elapsed_ms || 0);
        card.append(el("span", "badge", "Checking"), el("p", "claim", claim), live.wrap);
        pending.set(claim, { card, timer: live.timer });
        feed.append(card);
      }
      break;
    case "verdict": {
      const card = el("div", `card ${event.verdict}`);
      const pct = event.probability == null ? "" : ` · ${Math.round(event.probability * 100)}% likely true`;
      card.append(el("span", "badge", LABELS[event.verdict] + pct), el("p", "claim", event.claim));
      if (event.correction) {
        const fix = el("div", "fix");
        fix.append(el("span", "fix-label", "Right answer"), el("p", null, event.correction));
        card.append(fix);
      }
      const meta = el("div", "meta");
      if (event.url) {
        const a = el("a", null, event.source || new URL(event.url).hostname);
        a.href = event.url;
        a.target = "_blank";
        meta.append(a, " · ");
      }
      meta.append("after the words were heard");
      if (event.speaker) meta.append(` · speaker ${event.speaker}`);
      card.append(meta, meter(event.latency_ms, event.timings_ms));
      const placeholder = pending.get(event.claim);
      if (placeholder) {
        clearInterval(placeholder.timer);
        placeholder.card.replaceWith(card);
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
