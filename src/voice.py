"""Human-sounding spoken verdicts with AssemblyAI streaming text-to-speech.

One short WebSocket session per verdict: Generate the text, Terminate, and
collect the base64 PCM `Audio` frames into a WAV the browser can play.
"""

from __future__ import annotations

import base64
import io
import json
import os
import wave
from urllib.parse import urlencode

import aiohttp

TTS_URL = "wss://tts.us.assemblyai.com/v1/ws/"
VOICE = os.environ.get("BLINK_TTS_VOICE", "aai-kyla")
SAMPLE_RATE = 24000
MAX_CHARS = 400


def _wav(pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


async def synthesize(text: str, timeout: float = 15.0) -> bytes:
    """WAV bytes of `text` spoken by the AssemblyAI voice."""
    params = {
        "voice_id": VOICE,
        "language": "english",
        "model": "assemblyai-tts-1",
        "sample_rate": SAMPLE_RATE,
        "encoding": "pcm_s16le",
        "word_boundaries": "false",
    }
    pcm = bytearray()
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=timeout)
    ) as http:
        async with http.ws_connect(
            f"{TTS_URL}?{urlencode(params)}",
            headers={"Authorization": os.environ["ASSEMBLYAI_API_KEY"]},
        ) as ws:
            await ws.send_str(
                json.dumps({"type": "Generate", "text": text[:MAX_CHARS]})
            )
            await ws.send_str(json.dumps({"type": "Terminate"}))
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                kind = data.get("type")
                if kind == "Audio":
                    pcm += base64.b64decode(data["audio"])
                elif kind == "Termination":
                    break
                elif kind == "Error":
                    raise RuntimeError(
                        f"AssemblyAI TTS {data.get('error_code')}: {data.get('error')}"
                    )
    if not pcm:
        raise RuntimeError("AssemblyAI TTS returned no audio")
    return _wav(bytes(pcm))
