// BLINK website: capture a tab (getDisplayMedia) or the microphone, stream
// 16 kHz PCM to the server, and render captions, verdicts and latency meters.
import { render, clearFeed } from "/ui.js";

const $ = (id) => document.getElementById(id);
let ctx, stream, ws, worklet, source;
let speaking = false;

const store = {
  get(key, fallback) {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch { /* storage unavailable */ }
  },
};

$("speak").value = store.get("blink.speak", "all");
$("rate").value = store.get("blink.rate", "1.15");
$("speak").onchange = (e) => store.set("blink.speak", e.target.value);
$("rate").onchange = (e) => store.set("blink.rate", e.target.value);

const health = await fetch("/health").then((r) => r.json()).catch(() => ({}));
if (health.access_code_required) $("code-row").hidden = false;
$("code").value = store.get("blink.code", "");

async function capture(kind) {
  if (kind === "mic") {
    return navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  }
  if (!navigator.mediaDevices.getDisplayMedia) {
    throw new Error("This browser can't share tab audio. Use Chrome or Edge, or pick Microphone.");
  }
  const media = await navigator.mediaDevices.getDisplayMedia({
    video: true, // required by browsers; we drop it right away
    audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: false },
    selfBrowserSurface: "exclude",
    preferCurrentTab: false,
    systemAudio: "include",
  });
  media.getVideoTracks().forEach((t) => t.stop());
  if (!media.getAudioTracks().length) {
    media.getTracks().forEach((t) => t.stop());
    throw new Error('No audio was shared. Pick a tab and turn on "Also share tab audio".');
  }
  return media;
}

function speak(event) {
  const mode = $("speak").value;
  const flagged = event.verdict === "false" || event.verdict === "misleading";
  if (mode === "off" || (mode === "false" && !flagged) || !("speechSynthesis" in window)) return;
  const u = new SpeechSynthesisUtterance(event.speak);
  u.rate = Number($("rate").value);
  // In mic mode the mic would hear BLINK talking; mute the stream meanwhile.
  u.onstart = () => { speaking = true; };
  u.onend = u.onerror = () => { speaking = speechSynthesis.speaking; };
  speechSynthesis.speak(u);
}

async function start(kind) {
  clearFeed();
  stream = await capture(kind);
  ctx = new AudioContext({ latencyHint: "interactive" });
  await ctx.resume(); // some browsers start contexts suspended
  await ctx.audioWorklet.addModule("/pcm-worklet.js");
  source = ctx.createMediaStreamSource(stream);
  worklet = new AudioWorkletNode(ctx, "pcm16-downsampler");
  source.connect(worklet); // not to ctx.destination: no echo of the tab/mic

  const code = $("code").value.trim();
  if (code) store.set("blink.code", code);
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const qs = code ? `?code=${encodeURIComponent(code)}` : "";
  ws = new WebSocket(`${proto}://${location.host}/stream${qs}`);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => render({ type: "status", status: "connected" });
  ws.onmessage = (e) => {
    const event = JSON.parse(e.data);
    render(event);
    if (event.type === "verdict") speak(event);
  };
  ws.onerror = () => render({ type: "error", message: "Lost the connection to the BLINK server." });
  ws.onclose = () => { render({ type: "closed" }); stop(); };

  const silence = new ArrayBuffer(1600);
  worklet.port.onmessage = (e) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(kind === "mic" && speaking ? silence : e.data);
    }
  };
  stream.getAudioTracks()[0].onended = () => stop(); // "Stop sharing" clicked
  $("start-form").hidden = true;
  $("stop").hidden = false;
}

async function stop() {
  if (ws) {
    if (ws.readyState === WebSocket.OPEN) ws.send("stop");
    ws = null;
  }
  if (stream) stream.getTracks().forEach((t) => t.stop());
  if (ctx) await ctx.close().catch(() => {});
  ctx = stream = worklet = source = null;
  if ("speechSynthesis" in window) speechSynthesis.cancel();
  $("start-form").hidden = false;
  $("stop").hidden = true;
}

$("start-form").onsubmit = async (e) => {
  e.preventDefault();
  const kind = new FormData(e.target).get("source");
  $("start").disabled = true;
  try {
    await start(kind);
  } catch (err) {
    await stop();
    render({ type: "error", message: err.message || String(err) });
  } finally {
    $("start").disabled = false;
  }
};
$("stop").onclick = () => stop();
