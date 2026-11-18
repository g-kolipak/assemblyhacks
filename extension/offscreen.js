// Captures the tab's audio, keeps it audible, and streams 16 kHz PCM to the
// local fact-check server. Server events are forwarded to the extension.

let ctx, stream, ws, worklet, gain;

function emit(event) {
  chrome.runtime.sendMessage({ target: "background", type: "event", event });
}

async function start({ streamId, server }) {
  await stop();
  stream = await navigator.mediaDevices.getUserMedia({
    audio: { mandatory: { chromeMediaSource: "tab", chromeMediaSourceId: streamId } },
    video: false,
  });
  ctx = new AudioContext({ latencyHint: "interactive" });
  const source = ctx.createMediaStreamSource(stream);

  // Capturing mutes the tab, so play it back (through a gain node for ducking).
  gain = ctx.createGain();
  source.connect(gain).connect(ctx.destination);

  await ctx.audioWorklet.addModule("pcm-worklet.js");
  worklet = new AudioWorkletNode(ctx, "pcm16-downsampler");
  source.connect(worklet);

  ws = new WebSocket(server);
  ws.binaryType = "arraybuffer";
  ws.onopen = () => emit({ type: "status", status: "connected" });
  ws.onmessage = (e) => emit(JSON.parse(e.data));
  ws.onerror = () =>
    emit({
      type: "error",
      message: `Can't reach the fact-check server at ${server}. Is stream_server.py running?`,
    });
  ws.onclose = () => emit({ type: "closed" });
  worklet.port.onmessage = (e) => {
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(e.data);
  };
}

async function stop() {
  if (ws) {
    if (ws.readyState === WebSocket.OPEN) ws.send("stop");
    ws = null;
  }
  if (stream) stream.getTracks().forEach((t) => t.stop());
  if (ctx) await ctx.close();
  ctx = stream = worklet = gain = null;
}

chrome.runtime.onMessage.addListener((msg) => {
  if (msg.target !== "offscreen") return;
  if (msg.type === "start") start(msg).catch((err) => emit({ type: "error", message: String(err) }));
  if (msg.type === "stop") stop();
  if (msg.type === "duck" && gain && ctx) {
    gain.gain.setTargetAtTime(msg.on ? 0.25 : 1, ctx.currentTime, 0.05);
  }
});
