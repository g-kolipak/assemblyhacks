// Toolbar click toggles fact-checking for the current tab.
// Audio capture runs in an offscreen document (MV3 service workers can't use
// getUserMedia); verdicts are spoken here with chrome.tts.

const OFFSCREEN = "offscreen.html";
let state = { tabId: null, active: false };

async function ensureOffscreen() {
  const existing = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
  });
  if (existing.length) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN,
    reasons: ["USER_MEDIA", "AUDIO_PLAYBACK"],
    justification: "Capture tab audio to transcribe and fact-check it.",
  });
}

async function settings() {
  const s = await chrome.storage.local.get({
    server: "ws://127.0.0.1:8765/stream",
    speak: "all", // all | false | off
    rate: 1.15,
  });
  return s;
}

async function start(tab) {
  const streamId = await chrome.tabCapture.getMediaStreamId({
    targetTabId: tab.id,
  });
  await ensureOffscreen();
  const { server } = await settings();
  state = { tabId: tab.id, active: true };
  await chrome.storage.session.set({ events: [], active: true, title: tab.title });
  chrome.action.setBadgeText({ tabId: tab.id, text: "ON" });
  chrome.action.setBadgeBackgroundColor({ color: "#d93025" });
  chrome.runtime.sendMessage({ target: "offscreen", type: "start", streamId, server });
}

async function stop() {
  if (state.tabId !== null) chrome.action.setBadgeText({ tabId: state.tabId, text: "" });
  state = { tabId: null, active: false };
  chrome.tts.stop();
  await chrome.storage.session.set({ active: false });
  chrome.runtime.sendMessage({ target: "offscreen", type: "stop" }).catch(() => {});
}

chrome.action.onClicked.addListener((tab) => {
  // sidePanel.open must run synchronously inside the user gesture.
  chrome.sidePanel.open({ tabId: tab.id }).catch(() => {});
  (state.active ? stop() : start(tab)).catch((err) => {
    console.error(err);
    record({ type: "error", message: String(err.message || err) });
  });
});

chrome.tabs.onRemoved.addListener((tabId) => {
  if (tabId === state.tabId) stop();
});

async function record(event) {
  if (event.type === "partial") return; // captions only live in the panel
  const { events = [] } = await chrome.storage.session.get("events");
  events.push(event);
  await chrome.storage.session.set({ events: events.slice(-200) });
}

function duck(on) {
  chrome.runtime.sendMessage({ target: "offscreen", type: "duck", on }).catch(() => {});
}

async function speak(event) {
  const { speak: mode, rate } = await settings();
  const flagged = event.verdict === "false" || event.verdict === "misleading";
  if (mode === "off" || (mode === "false" && !flagged)) return;
  chrome.tts.speak(event.speak, {
    enqueue: true,
    rate,
    onEvent: (e) => {
      if (e.type === "start") duck(true);
      if (["end", "interrupted", "cancelled", "error"].includes(e.type)) duck(false);
    },
  });
}

chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
  if (msg.target === "background" && msg.type === "event") {
    record(msg.event);
    if (msg.event.type === "verdict") speak(msg.event);
    if (msg.event.type === "closed" && state.active) stop();
  }
  if (msg.target === "background" && msg.type === "stop") stop();
  if (msg.target === "background" && msg.type === "state") reply(state);
});
