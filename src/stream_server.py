"""Streaming fact-check backend for the Chrome extension.

The extension captures a tab's audio (YouTube, podcasts, anything) and streams
16 kHz mono PCM over a WebSocket to this server. The server:

  1. Forwards the audio to AssemblyAI streaming speech-to-text.
  2. Cuts the live transcript into sentences as soon as they are final. Long
     monologues are flushed mid-turn, so checks never wait for a pause.
  3. Runs each sentence through `FactChecker` (claim extraction, one Parallel
     Decisions call, a correction for false claims) in the background.
  4. Streams captions and verdicts back to the extension as JSON.

Keys stay on this machine; the extension never sees them.

    uv run python src/stream_server.py            # ws://127.0.0.1:8765/stream
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from aiohttp import web
from dotenv import dotenv_values, load_dotenv

from fact_check import CheckResult, FactChecker, gateway_chat
from parallel_api import ParallelAPI

load_dotenv(".env.local")

logger = logging.getLogger("factcheck.server")

defaults = dotenv_values(Path(__file__).with_name("gateway.env"))
LLM_MODEL = os.environ.get("LLM_MODEL", defaults["LLM_MODEL"])
STT_MODEL = os.environ.get("ASSEMBLYAI_STT_MODEL", "universal-3-6-pro")
AAI_URL = "wss://streaming.assemblyai.com/v3/ws"
SAMPLE_RATE = 16000

# A sentence is sent for checking once it has at least this many words.
MIN_SENTENCE_WORDS = int(os.environ.get("FACTCHECK_MIN_WORDS", "5"))
# AssemblyAI accepts 20-1000 ms per audio message.
MAX_FRAME_BYTES = SAMPLE_RATE * 2  # one second of 16-bit mono
SENTENCE_END = re.compile(r"[.?!][\"')\]]*$")


def assemblyai_url() -> str:
    params = {
        "sample_rate": SAMPLE_RATE,
        "encoding": "pcm_s16le",
        "speech_model": STT_MODEL,
        "format_turns": "true",
        # Short silences end turns quickly; podcasts rarely pause for long.
        "min_turn_silence": os.environ.get("AAI_MIN_TURN_SILENCE", "160"),
        "max_turn_silence": os.environ.get("AAI_MAX_TURN_SILENCE", "1000"),
        "speaker_labels": "true",
        # Extra partials every ~3 s during long monologues, so sentences can
        # be checked before the speaker pauses.
        "continuous_partials": "true",
    }
    return f"{AAI_URL}?{urlencode(params)}"


class TurnSegmenter:
    """Turn AssemblyAI `Turn` messages into sentences ready to fact-check.

    Words in a turn are cumulative. Universal-3 Pro only marks words final at
    the end of a turn, but its partials are punctuated, so a sentence that is
    already followed by more words is finished and can be checked right away,
    mid-monologue. The rest of the turn is emitted at end of turn.
    """

    def __init__(self, min_words: int = MIN_SENTENCE_WORDS) -> None:
        self.min_words = min_words
        self._turn: object = None
        self._done = 0
        self._closed: set[object] = set()

    def feed(self, msg: dict) -> list[str]:
        turn = msg.get("turn_order")
        if turn in self._closed:
            return []  # e.g. a formatted copy of a turn we already flushed
        if turn != self._turn:
            self._turn, self._done = turn, 0
        words = msg.get("words") or []

        if msg.get("end_of_turn"):
            self._closed.add(turn)
            text = msg.get("transcript") or " ".join(w["text"] for w in words)
            if self._done:
                # Part of this turn was already flushed; send only the rest.
                text = " ".join(w["text"] for w in words[self._done :])
            self._done = len(words)
            return [text.strip()] if text.strip() else []

        pending = [w["text"] for w in words[self._done :]]
        # The last word may still be revised unless it is final.
        settled = len(pending) - (0 if words and words[-1].get("word_is_final") else 1)
        cut = max(
            (i for i, t in enumerate(pending[:settled]) if SENTENCE_END.search(t)),
            default=-1,
        )
        if cut + 1 < self.min_words:
            return []
        self._done += cut + 1
        return [" ".join(pending[: cut + 1])]


def result_event(result: CheckResult, speaker: str | None, heard_at: float) -> dict:
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
        "timings_ms": {k: round(v * 1000) for k, v in result.timings.items()},
    }


def origin_allowed(origin: str | None) -> bool:
    """Only the extension (or a local tool with no Origin) may connect."""
    allowed = os.environ.get("FACTCHECK_ALLOWED_ORIGIN")
    if origin is None:
        return True
    if allowed:
        return origin == allowed
    return origin.startswith("chrome-extension://")


async def stream_handler(request: web.Request) -> web.WebSocketResponse:
    if not origin_allowed(request.headers.get("Origin")):
        raise web.HTTPForbidden(text="Origin not allowed")
    client = web.WebSocketResponse(heartbeat=20)
    await client.prepare(request)
    outbox: asyncio.Queue[dict] = asyncio.Queue()
    parallel = ParallelAPI()
    checker = FactChecker(gateway_chat(LLM_MODEL), parallel.decide_raw)
    segmenter = TurnSegmenter()
    tasks: set[asyncio.Task] = set()

    async def send_loop() -> None:
        while True:
            await client.send_json(await outbox.get())

    async def check(text: str, speaker: str | None) -> None:
        heard_at = time.perf_counter()
        outbox.put_nowait({"type": "sentence", "text": text, "speaker": speaker})
        try:
            await checker.process(
                text,
                lambda r: outbox.put_nowait(result_event(r, speaker, heard_at)),
            )
        except Exception as e:
            logger.exception("Fact check failed")
            outbox.put_nowait({"type": "error", "message": f"Fact check failed: {e}"})

    async def from_assemblyai(aai: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in aai:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            kind = data.get("type")
            if kind == "Begin":
                outbox.put_nowait({"type": "status", "status": "listening"})
            elif kind == "Turn":
                speaker = data.get("speaker_label")
                speaker = None if speaker in (None, "UNKNOWN") else speaker
                if not data.get("end_of_turn"):
                    words = data.get("words") or []
                    outbox.put_nowait(
                        {
                            "type": "partial",
                            "text": " ".join(w["text"] for w in words),
                            "speaker": speaker,
                        }
                    )
                for text in segmenter.feed(data):
                    task = asyncio.create_task(check(text, speaker))
                    tasks.add(task)
                    task.add_done_callback(tasks.discard)
            elif kind == "Termination":
                break
            elif "error" in data:
                outbox.put_nowait({"type": "error", "message": str(data["error"])})

    sender = asyncio.create_task(send_loop())
    try:
        async with aiohttp.ClientSession() as http:
            aai = await http.ws_connect(
                assemblyai_url(),
                headers={"Authorization": os.environ["ASSEMBLYAI_API_KEY"]},
            )
            reader = asyncio.create_task(from_assemblyai(aai))
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
        if tasks:
            await asyncio.wait(tasks, timeout=10)
        while not outbox.empty():
            await client.send_json(outbox.get_nowait())
        sender.cancel()
        await parallel.aclose()
        await client.close()
    return client


async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "stt_model": STT_MODEL, "llm": LLM_MODEL})


def build_app() -> web.Application:
    app = web.Application()
    app.add_routes([web.get("/stream", stream_handler), web.get("/health", health)])
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    missing = [
        k for k in ("ASSEMBLYAI_API_KEY", "PARALLEL_API_KEY") if not os.getenv(k)
    ]
    if missing:
        raise SystemExit(f"Set {', '.join(missing)} in .env.local first.")
    port = int(os.environ.get("FACTCHECK_PORT", "8765"))
    # Bind to localhost only: this server spends your API credits.
    web.run_app(build_app(), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
