"""Streaming fact-check backend for the Chrome extension.

The extension captures a tab's audio (YouTube, podcasts, anything) and streams
16 kHz mono PCM over a WebSocket to this server. The server:

  1. Forwards the audio to AssemblyAI streaming speech-to-text.
  2. Watches the transcript word by word. Every couple of new words, the live
     window goes to claim detection; it never waits for a sentence to end.
  3. The moment a complete claim appears, it fires one Parallel Decisions
     call for it (plus a correction for false claims) in the background.
  4. Streams captions and verdicts back to the extension as JSON.

It also serves the BLINK website at / (tab sharing or microphone instead of
the extension). Keys stay on the server; browsers never see them.

    uv run python src/stream_server.py            # http://127.0.0.1:8765
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import time
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from aiohttp import web
from dotenv import dotenv_values, load_dotenv

import url_audio
import voice
from fact_check import CheckResult, FactChecker, gateway_chat
from parallel_api import ParallelAPI

load_dotenv(".env.local")

logger = logging.getLogger("factcheck.server")

defaults = dotenv_values(Path(__file__).with_name("gateway.env"))
LLM_MODEL = os.environ.get("LLM_MODEL", defaults["LLM_MODEL"])
# Extra gateway models used when LLM_MODEL's per-minute budget is spent.
LLM_FALLBACKS = os.environ.get("BLINK_LLM_FALLBACKS", "qwen3.5-4b-32k-fast")
# Word-by-word partials (~60 messages per 15 s of speech versus ~20 for
# Universal-3 Pro), so claims are visible as soon as their last word is said.
STT_MODEL = os.environ.get("BLINK_STT_MODEL", "universal-streaming-english")
U3_PRO_MODELS = ("u3-rt-pro", "universal-3-5-pro", "universal-3-6-pro")
AAI_URL = "wss://streaming.assemblyai.com/v3/ws"
SAMPLE_RATE = 16000

# Run claim detection after this many new words. Detection is coalesced (one
# call in flight, always on the newest words), so a small step costs little.
STEP_WORDS = int(os.environ.get("BLINK_STEP_WORDS", "2"))
# Words of transcript the detector sees: the minimum that fits one long claim
# plus a little context. Measured live: 45 words -> 0.89 s median detection,
# 15 words -> 0.51 s. Claims already checked are excluded, so older speech
# is not needed.
WINDOW_WORDS = int(os.environ.get("BLINK_WINDOW_WORDS", "20"))
# AssemblyAI accepts 20-1000 ms per audio message.
MAX_FRAME_BYTES = SAMPLE_RATE * 2  # one second of 16-bit mono


def assemblyai_url() -> str:
    params: dict[str, object] = {
        "sample_rate": SAMPLE_RATE,
        "encoding": "pcm_s16le",
        "speech_model": STT_MODEL,
        "format_turns": "true",
        # Short silences end turns quickly; podcasts rarely pause for long.
        "min_turn_silence": os.environ.get("AAI_MIN_TURN_SILENCE", "160"),
        "max_turn_silence": os.environ.get("AAI_MAX_TURN_SILENCE", "1000"),
    }
    if STT_MODEL in U3_PRO_MODELS:
        params.update(speaker_labels="true", continuous_partials="true")
    return f"{AAI_URL}?{urlencode(params)}"


class LiveWindow:
    """Rolling transcript window built from AssemblyAI `Turn` messages.

    Words inside a turn are cumulative and may be revised until the turn ends,
    so each message replaces the current turn. Finished turns move to history.
    `feed` returns the new window once at least `step` words were added.
    """

    def __init__(self, step: int = STEP_WORDS, size: int = WINDOW_WORDS) -> None:
        self.step, self.size = step, size
        self._history: list[str] = []
        self._current: list[str] = []
        self._turn: object = None
        self._closed: set[object] = set()
        self._emitted_at = 0  # total word count at the last emitted window

    @property
    def total(self) -> int:
        return len(self._history) + len(self._current)

    def text(self) -> str:
        return " ".join((self._history + self._current)[-self.size :])

    def feed(self, msg: dict) -> str | None:
        turn = msg.get("turn_order")
        if turn in self._closed:
            return None  # formatted copy of a finished turn
        words = [w["text"] for w in msg.get("words") or []]
        if msg.get("end_of_turn") and msg.get("transcript"):
            words = msg["transcript"].split()
        self._turn, self._current = turn, words
        ended = bool(msg.get("end_of_turn"))
        if ended:
            self._closed.add(turn)
            self._history = (self._history + self._current)[-self.size :]
            self._current = []
        if ended or self.total - self._emitted_at >= self.step:
            self._emitted_at = self.total
            return self.text()
        return None


class ClaimWatcher:
    """Run live claim detection continuously, one call at a time.

    `update` never blocks. If detection is busy, the newest window waits and
    replaces any older waiting window, so the detector always reads the latest
    words and requests never pile up. New claims go straight to `on_claims`.
    """

    def __init__(self, checker: FactChecker, on_claims) -> None:
        self._checker = checker
        self._on_claims = on_claims
        self._pending: tuple[str, float] | None = None
        self._task: asyncio.Task | None = None

    def update(self, window: str) -> None:
        self._pending = (window, time.perf_counter())
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while self._pending is not None:
            window, heard_at = self._pending
            self._pending = None
            try:
                claims = await self._checker.extract_live(window)
            except Exception:
                logger.exception("Live claim detection failed")
                continue
            if claims:
                self._on_claims(claims, heard_at, time.perf_counter() - heard_at)

    async def drain(self) -> None:
        if self._task is not None:
            await self._task


def result_event(
    result: CheckResult,
    speaker: str | None,
    heard_at: float,
    detect_s: float = 0.0,
) -> dict:
    """Verdict message. `latency_ms` runs from when the claim's last words
    arrived from AssemblyAI; `timings_ms` breaks it down for the meter."""
    timings = {"detect": detect_s, **result.timings}
    return {
        "type": "verdict",
        "claim": result.claim,
        "verdict": result.verdict,
        "probability": result.probability,
        "correction": result.correction,
        "source": result.source,
        "url": result.url,
        "speak": result.spoken(),
        "speaker": speaker,
        "latency_ms": round((time.perf_counter() - heard_at) * 1000),
        "timings_ms": {k: round(v * 1000) for k, v in timings.items()},
    }


ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT / "web"
EXTENSION_DIR = ROOT / "extension"
# Website files: the page plus UI, audio and styles shared with the extension.
STATIC_FILES = {
    "/": WEB_DIR / "index.html",
    "/app.js": WEB_DIR / "app.js",
    "/site.css": WEB_DIR / "site.css",
    "/ui.js": EXTENSION_DIR / "ui.js",
    "/pcm-worklet.js": EXTENSION_DIR / "pcm-worklet.js",
    "/sidepanel.css": EXTENSION_DIR / "sidepanel.css",
    "/scene.js": WEB_DIR / "scene.js",
    "/img/logo.png": WEB_DIR / "img" / "logo.png",
    "/img/wordmark.png": WEB_DIR / "img" / "wordmark.png",
    "/img/icon.png": WEB_DIR / "img" / "icon.png",
    "/img/favicon.png": WEB_DIR / "img" / "favicon.png",
    "/img/og.png": WEB_DIR / "img" / "og.png",
}

# Guard rails for a public deployment: every session spends API credits.
ACCESS_CODE = os.environ.get("BLINK_ACCESS_CODE", "")
MAX_SESSIONS = int(os.environ.get("BLINK_MAX_SESSIONS", "3"))
MAX_SESSION_SECONDS = int(os.environ.get("BLINK_MAX_SESSION_MIN", "30")) * 60
_active_sessions = 0


def origin_allowed(origin: str | None, host: str | None = None) -> bool:
    """The extension, this site itself, or a local tool with no Origin."""
    allowed = os.environ.get("FACTCHECK_ALLOWED_ORIGIN")
    if origin is None:
        return True
    if host and origin.split("://", 1)[-1] == host:
        return True  # the BLINK website served by this server
    if allowed:
        return origin == allowed  # pin one extension ID
    return origin.startswith("chrome-extension://")


def code_ok(code: str | None) -> bool:
    if not ACCESS_CODE:
        return True
    return hmac.compare_digest((code or "").encode(), ACCESS_CODE.encode())


async def stream_handler(request: web.Request) -> web.WebSocketResponse:
    global _active_sessions
    if not origin_allowed(request.headers.get("Origin"), request.host):
        raise web.HTTPForbidden(text="Origin not allowed")
    client = web.WebSocketResponse(heartbeat=20)
    await client.prepare(request)
    if not code_ok(request.query.get("code")):
        await client.send_json({"type": "error", "message": "Wrong access code."})
        await client.close()
        return client
    if _active_sessions >= MAX_SESSIONS:
        await client.send_json(
            {"type": "error", "message": "BLINK is busy right now. Try again soon."}
        )
        await client.close()
        return client
    video_id = None
    if request.query.get("youtube"):
        video_id = url_audio.youtube_id(request.query["youtube"])
        if not video_id:
            await client.send_json(
                {"type": "error", "message": "That isn't a YouTube video link."}
            )
            await client.close()
            return client
    speak_mode = request.query.get("speak", "all")
    if speak_mode not in ("all", "false", "off"):
        speak_mode = "all"
    _active_sessions += 1
    try:
        return await _run_session(client, video_id, speak_mode)
    finally:
        _active_sessions -= 1


async def _stream_youtube(
    client: web.WebSocketResponse,
    aai: aiohttp.ClientWebSocketResponse,
    video_id: str,
    outbox: asyncio.Queue[dict],
) -> None:
    """Feed a YouTube video's audio to AssemblyAI, following the viewer's
    embed: "play:<seconds>" starts or seeks, "pause" holds, "stop" ends."""
    try:
        media = await url_audio.resolve(video_id)
    except Exception as e:
        reason = str(e).replace("ERROR: ", "").splitlines()[0][:200]
        logger.warning("Could not load YouTube video %s: %s", video_id, reason)
        if "not a bot" in reason or "Sign in" in reason:
            message = (
                "YouTube is blocking blink's server from loading videos right now. "
                "Upload the video file instead, or pick “A browser tab”."
            )
        else:
            message = f"Couldn't load that YouTube video: {reason}"
        outbox.put_nowait({"type": "error", "message": message})
        return
    player = url_audio.Player(media)
    outbox.put_nowait(
        {
            "type": "media",
            "video_id": video_id,
            "title": media.title,
            "duration": media.duration,
        }
    )

    async def controls() -> None:
        async for msg in client:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            cmd, _, arg = msg.data.partition(":")
            if cmd == "stop":
                return
            if cmd == "pause":
                player.pause()
            elif cmd == "play":
                try:
                    at = float(arg or 0)
                except ValueError:
                    continue
                await player.play(at)

    async def pump() -> None:
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        while not aai.closed:
            await aai.send_bytes(await player.chunk())
            if player.ended:
                outbox.put_nowait({"type": "ended"})
                return
            next_at += url_audio.CHUNK_S
            await asyncio.sleep(max(0.0, next_at - loop.time()))

    jobs = {asyncio.create_task(controls()), asyncio.create_task(pump())}
    try:
        await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for job in jobs:
            job.cancel()
        await player.close()


async def _run_session(
    client: web.WebSocketResponse,
    video_id: str | None = None,
    speak_mode: str = "all",
) -> web.WebSocketResponse:
    outbox: asyncio.Queue[dict] = asyncio.Queue()
    parallel = ParallelAPI()
    checker = FactChecker(
        gateway_chat(f"{LLM_MODEL},{LLM_FALLBACKS}"), parallel.decide_raw
    )
    window = LiveWindow()
    tasks: set[asyncio.Task] = set()
    speaker: str | None = None

    async def send_loop() -> None:
        while True:
            await client.send_json(await outbox.get())

    async def check(
        claims: list[str], heard_at: float, detect_s: float, who: str | None
    ) -> None:
        outbox.put_nowait(
            {
                "type": "checking",
                "claims": claims,
                "speaker": who,
                "elapsed_ms": round(detect_s * 1000),
            }
        )

        def emit(result: CheckResult) -> None:
            event = result_event(result, who, heard_at, detect_s)
            outbox.put_nowait(event)
            flagged = event["verdict"] in ("false", "misleading")
            if speak_mode == "all" or (speak_mode == "false" and flagged):
                task = asyncio.create_task(say(event))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

        try:
            await checker.check_claims(claims, emit)
        except Exception as e:
            logger.exception("Fact check failed")
            outbox.put_nowait({"type": "error", "message": f"Fact check failed: {e}"})

    async def say(event: dict) -> None:
        # Human voice from AssemblyAI TTS; the browser voice is the fallback.
        try:
            wav = await voice.synthesize(event["speak"])
            outbox.put_nowait(
                {
                    "type": "speech",
                    "claim": event["claim"],
                    "audio": base64.b64encode(wav).decode(),
                }
            )
        except Exception as e:
            logger.warning("AssemblyAI TTS failed: %s", e)
            outbox.put_nowait(
                {"type": "speech", "claim": event["claim"], "fallback": event["speak"]}
            )

    def on_claims(claims: list[str], heard_at: float, detect_s: float) -> None:
        # Fire the Decisions call immediately; detection keeps running.
        task = asyncio.create_task(check(claims, heard_at, detect_s, speaker))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    watcher = ClaimWatcher(checker, on_claims)

    async def from_assemblyai(aai: aiohttp.ClientWebSocketResponse) -> None:
        nonlocal speaker
        async for msg in aai:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            kind = data.get("type")
            if kind == "Begin":
                outbox.put_nowait({"type": "status", "status": "listening"})
            elif kind == "Turn":
                label = data.get("speaker_label")
                speaker = None if label in (None, "UNKNOWN") else label
                if not data.get("end_of_turn"):
                    words = data.get("words") or []
                    outbox.put_nowait(
                        {
                            "type": "partial",
                            "text": " ".join(w["text"] for w in words),
                            "speaker": speaker,
                        }
                    )
                text = window.feed(data)
                if text:
                    watcher.update(text)
            elif kind == "Termination":
                break
            elif "error" in data:
                outbox.put_nowait({"type": "error", "message": str(data["error"])})

    async def time_limit() -> None:
        await asyncio.sleep(MAX_SESSION_SECONDS)
        outbox.put_nowait(
            {"type": "error", "message": "Session time limit reached. Start again."}
        )
        await asyncio.sleep(0.2)
        await client.close()

    sender = asyncio.create_task(send_loop())
    limiter = asyncio.create_task(time_limit())
    try:
        async with aiohttp.ClientSession() as http:
            aai = await http.ws_connect(
                assemblyai_url(),
                headers={"Authorization": os.environ["ASSEMBLYAI_API_KEY"]},
            )
            reader = asyncio.create_task(from_assemblyai(aai))
            if video_id:
                await _stream_youtube(client, aai, video_id, outbox)
            else:
                async for msg in client:
                    if aai.closed:
                        break
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        data = msg.data
                        for i in range(0, len(data), MAX_FRAME_BYTES):
                            await aai.send_bytes(data[i : i + MAX_FRAME_BYTES])
                    elif msg.type == aiohttp.WSMsgType.TEXT and msg.data == "stop":
                        break
            if not aai.closed:
                await aai.send_str(json.dumps({"type": "Terminate"}))
            await asyncio.wait_for(reader, timeout=5)
    except aiohttp.WSServerHandshakeError as e:
        logger.error("AssemblyAI rejected the connection: %s", e)
        await client.send_json({"type": "error", "message": f"AssemblyAI: {e}"})
    except (TimeoutError, asyncio.CancelledError):
        pass
    finally:
        await watcher.drain()
        if tasks:
            await asyncio.wait(tasks, timeout=10)
        while not outbox.empty():
            await client.send_json(outbox.get_nowait())
        sender.cancel()
        limiter.cancel()
        await parallel.aclose()
        await client.close()
    return client


async def health(_: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "stt_model": STT_MODEL,
            "llm": LLM_MODEL,
            "access_code_required": bool(ACCESS_CODE),
        }
    )


def _static(path: Path):
    async def handler(_: web.Request) -> web.FileResponse:
        return web.FileResponse(path, headers={"Cache-Control": "no-cache"})

    return handler


def build_app() -> web.Application:
    app = web.Application()
    app.add_routes([web.get("/stream", stream_handler), web.get("/health", health)])
    for route, path in STATIC_FILES.items():
        app.router.add_get(route, _static(path))
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    missing = [
        k for k in ("ASSEMBLYAI_API_KEY", "PARALLEL_API_KEY") if not os.getenv(k)
    ]
    if missing:
        raise SystemExit(f"Set {', '.join(missing)} in .env.local first.")
    # On Railway (PORT is set) listen publicly; locally, localhost only.
    hosted = "PORT" in os.environ
    port = int(os.environ.get("PORT") or os.environ.get("FACTCHECK_PORT", "8765"))
    if hosted and not ACCESS_CODE:
        logger.warning("BLINK_ACCESS_CODE is not set: anyone can spend your credits.")
    web.run_app(build_app(), host="0.0.0.0" if hosted else "127.0.0.1", port=port)


if __name__ == "__main__":
    main()
