import { render } from "./ui.js";

const $ = (id) => document.getElementById(id);

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
