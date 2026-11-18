// blink website: capture a video/audio file, a browser tab (getDisplayMedia) or
// the microphone, stream 16 kHz PCM to the server, and render captions,
// verdicts and latency meters. The 3D eye (scene.js) reacts to all of it.
import { render, clearFeed } from "/ui.js";

const $ = (id) => document.getElementById(id);
let ctx, stream, ws, worklet, source, analyser, levelTimer;
let speaking = false;
let pendingChecks = 0;
let objectUrl = null;
let runId = 0;

const noop = () => {};
let eye = { setListening: noop, setChecking: noop, setLevel: noop, verdict: noop, poke: noop };
import("/scene.js")
  .then((m) => { eye = m.createEye($("scene"), $("eye-slot"), $("feed")); })
  .catch((err) => console.warn("3D eye unavailable:", err));
$("eye-slot").onclick = () => eye.poke();

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
if (health.access_code_required) {
  $("code-row").hidden = false;
  $("code").value = store.get("blink.code", "");
}

function chosenSource() {
  return new FormData($("start-form")).get("source");
}
function syncSourceUI() {
  $("file-row").hidden = chosenSource() !== "file";
  $("url-row").hidden = chosenSource() !== "youtube";
}

// YouTube: the server fetches the audio itself (no screen sharing). The embed
// here reports play/pause/seek so the server hears what the viewer watches.
let ytPlayer = null;
let ytApi = null;
function loadYouTube() {
  ytApi ??= new Promise((resolve, reject) => {
    window.onYouTubeIframeAPIReady = () => resolve(window.YT);
    const s = document.createElement("script");
    s.src = "https://www.youtube.com/iframe_api";
    s.onerror = () => { ytApi = null; reject(new Error("Couldn't load the YouTube player.")); };
    document.head.append(s);
  });
  return ytApi;
}

async function showVideo(videoId) {
  const YT = await loadYouTube();
  if (ytPlayer) ytPlayer.destroy();
  $("yt-wrap").hidden = false;
  const send = (msg) => { if (ws && ws.readyState === WebSocket.OPEN) ws.send(msg); };
  ytPlayer = new YT.Player("yt", {
    videoId,
    playerVars: { playsinline: 1, rel: 0, autoplay: 1 },
    events: {
      onReady: (e) => e.target.playVideo(),
      onStateChange: (e) => {
        if (e.data === YT.PlayerState.PLAYING) send(`play:${e.target.getCurrentTime().toFixed(2)}`);
        else if (e.data === YT.PlayerState.PAUSED || e.data === YT.PlayerState.BUFFERING) send("pause");
      },
    },
  });
}
$("start-form").addEventListener("change", syncSourceUI);
syncSourceUI();

const tally = { true: 0, false: 0, misleading: 0, unverified: 0 };
function count(verdict) {
  if (!(verdict in tally)) return;
  tally[verdict] += 1;
  $(`n-${verdict}`).textContent = tally[verdict];
}
function resetTally() {
  for (const k of Object.keys(tally)) { tally[k] = 0; $(`n-${k}`).textContent = "0"; }
}

async function capture(kind) {
  if (kind === "mic") {
    return navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
  }
  if (!navigator.mediaDevices.getDisplayMedia) {
    throw new Error("This browser can't share tab audio. Use Chrome or Edge, or upload a file instead.");
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

// An uploaded file plays in the page; its audio goes both to the speakers
// and to the fact-checker, exactly like a shared tab.
function fileSource(file) {
  // A media element can feed only one AudioContext ever, so every run gets a
  // fresh <video> element.
  const old = $("player");
  const player = old.cloneNode(false);
  player.removeAttribute("src");
  old.replaceWith(player);
  if (objectUrl) URL.revokeObjectURL(objectUrl);
  objectUrl = URL.createObjectURL(file);
  player.src = objectUrl;
  player.hidden = false;
  const node = ctx.createMediaElementSource(player);
  node.connect(ctx.destination);
  player.onended = () => {
    render({ type: "status", status: "connected" });
    $("status").textContent = "Video finished. Wrapping up the last checks…";
    stop();
  };
  return node;
}

// Verdicts are spoken by an AssemblyAI voice: the server sends each clip as a
// "speech" event and they play one after another. If TTS failed, the
// browser's own voice reads the text instead.
const voiceQueue = [];
let voiceBusy = false;

function playNextVoice() {
  if (voiceBusy || !voiceQueue.length) return;
  const clip = voiceQueue.shift();
  voiceBusy = true;
  speaking = true; // in mic mode, don't let the mic hear blink talking
  const done = () => { voiceBusy = false; speaking = false; playNextVoice(); };
  const rate = Number($("rate").value);
  if (clip.audio) {
    const audio = new Audio(`data:audio/wav;base64,${clip.audio}`);
    audio.playbackRate = rate;
    audio.onended = audio.onerror = done;
    audio.play().catch(done);
  } else if (clip.fallback && "speechSynthesis" in window) {
    const u = new SpeechSynthesisUtterance(clip.fallback);
    u.rate = rate;
    u.onend = u.onerror = done;
    speechSynthesis.speak(u);
  } else {
    done();
  }
}

function queueVoice(event) {
  if ($("speak").value === "off") return;
  voiceQueue.push(event);
  playNextVoice();
}

// Jeff's calls: each finished sentence, then whether Jeff sends it to Parallel.
function addLine(event) {
  const li = document.createElement("li");
  li.className = "line reading";
  li.id = `line-${event.id}`;
  const text = document.createElement("span");
  text.className = "line-text";
  text.textContent = event.text;
  const call = document.createElement("span");
  call.className = "call";
  call.textContent = "Jeff is reading";
  li.append(text, call);
  const log = $("jeff-log");
  log.append(li);
  while (log.children.length > 40) log.firstElementChild.remove();
}

function markLine(event) {
  const li = $(`line-${event.id}`);
  if (!li) return;
  li.classList.remove("reading");
  li.classList.add(event.check ? "flagged" : "skipped");
  const call = li.querySelector(".call");
  call.textContent = event.check ? "Checking it" : `Skipped: ${event.reason || "nothing to check"}`;
  call.title = event.reason || "";
}

function onEvent(event) {
  render(event);
  if (event.type === "status" && event.status === "listening") eye.setListening(true);
  if (event.type === "media") {
    $("status").textContent = `Watching “${event.title}”`;
    showVideo(event.video_id).catch((err) => render({ type: "error", message: err.message }));
  }
  if (event.type === "speech") queueVoice(event);
  if (event.type === "sentence") addLine(event);
  if (event.type === "jeff") markLine(event);
  if (event.type === "ended") {
    $("status").textContent = "Video finished. Wrapping up the last checks…";
  }
  if (event.type === "checking") {
    pendingChecks += event.claims.length;
    eye.setChecking(true);
  }
  if (event.type === "verdict") {
    pendingChecks = Math.max(0, pendingChecks - 1);
    eye.setChecking(pendingChecks > 0);
    eye.verdict(event.verdict);
    count(event.verdict);
  }
  if (event.type === "closed") {
    pendingChecks = 0;
    eye.setChecking(false);
    eye.setListening(false);
    document.body.classList.remove("live");
  }
}

function connect(params) {
  const code = $("code").value.trim();
  if (code) { store.set("blink.code", code); params.set("code", code); }
  params.set("speak", $("speak").value);
  voiceQueue.length = 0;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const qs = params.toString() ? `?${params}` : "";
  // Each run gets its own id. A stopped run's socket keeps delivering its last
  // verdicts until the server closes it, but must never touch a newer run.
  const run = ++runId;
  const sock = new WebSocket(`${proto}://${location.host}/stream${qs}`);
  ws = sock;
  sock.binaryType = "arraybuffer";
  sock.onmessage = (e) => { if (run === runId) onEvent(JSON.parse(e.data)); };
  sock.onerror = () => {
    if (run === runId) render({ type: "error", message: "Lost the connection to the blink server. Press start to try again." });
  };
  sock.onclose = () => {
    if (run !== runId) return;
    onEvent({ type: "closed" });
    if (ws === sock) stop();
  };
  document.body.classList.add("live");
  $("start-form").hidden = true;
  $("stop").hidden = false;
}

async function start(kind) {
  const file = $("file").files[0];
  const link = $("yt-url").value.trim();
  if (kind === "file" && !file) throw new Error("Choose a video or audio file first.");
  if (kind === "youtube" && !link) throw new Error("Paste a YouTube link first.");
  clearFeed();
  resetTally();
  $("jeff-log").replaceChildren();
  pendingChecks = 0;

  if (kind === "youtube") {
    $("player").hidden = true;
    $("status").textContent = "Loading the video…";
    connect(new URLSearchParams({ youtube: link }));
    return;
  }
  $("yt-wrap").hidden = true;
  if (ytPlayer) { ytPlayer.destroy(); ytPlayer = null; }

  ctx = new AudioContext({ latencyHint: "interactive" });
  await ctx.resume(); // some browsers start contexts suspended
  await ctx.audioWorklet.addModule("/pcm-worklet.js");
  if (kind === "file") {
    source = fileSource(file);
  } else {
    stream = await capture(kind);
    source = ctx.createMediaStreamSource(stream);
    stream.getAudioTracks()[0].onended = () => stop(); // "Stop sharing" clicked
  }
  worklet = new AudioWorkletNode(ctx, "pcm16-downsampler");
  source.connect(worklet); // tab/mic are not sent to the speakers: no echo

  analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);
  const buf = new Float32Array(analyser.fftSize);
  levelTimer = setInterval(() => {
    if (!analyser) return;
    analyser.getFloatTimeDomainData(buf);
    let sum = 0;
    for (const v of buf) sum += v * v;
    eye.setLevel(Math.sqrt(sum / buf.length));
  }, 50);

  connect(new URLSearchParams());
  ws.onopen = () => {
    onEvent({ type: "status", status: "connected" });
    if (kind === "file") $("player").play().catch(() => {});
  };

  const silence = new ArrayBuffer(1600);
  worklet.port.onmessage = (e) => {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(kind === "mic" && speaking ? silence : e.data);
    }
  };
}

async function stop() {
  if (ws) {
    if (ws.readyState === WebSocket.OPEN) ws.send("stop");
    ws = null;
  }
  clearInterval(levelTimer);
  if (stream) stream.getTracks().forEach((t) => t.stop());
  const player = $("player");
  if (!player.paused) player.pause();
  if (ytPlayer?.pauseVideo) ytPlayer.pauseVideo();
  if (ctx) await ctx.close().catch(() => {});
  ctx = stream = worklet = source = analyser = null;
  eye.setListening(false);
  if ($("speak").value === "off") voiceQueue.length = 0;
  $("start-form").hidden = false;
  $("stop").hidden = true;
}

$("start-form").onsubmit = async (e) => {
  e.preventDefault();
  const kind = chosenSource();
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
