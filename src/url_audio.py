"""Audio from a YouTube link, as 16 kHz mono PCM, paced like live playback.

yt-dlp resolves the link to a direct audio stream; ffmpeg decodes it. The
website plays the same video in an embed and reports play, pause and seeks,
so the fact-checker hears what the viewer is watching. Only YouTube links are
accepted, so the server cannot be pointed at arbitrary (internal) URLs.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger("factcheck.url")

SAMPLE_RATE = 16000
CHUNK_S = 0.1
CHUNK_BYTES = int(SAMPLE_RATE * 2 * CHUNK_S)  # 100 ms of 16-bit mono
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
}


def youtube_id(url: str) -> str | None:
    """The video ID of a YouTube watch/short/share link, else None."""
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or (u.hostname or "") not in YOUTUBE_HOSTS:
        return None
    if u.hostname == "youtu.be":
        vid = u.path.lstrip("/").split("/")[0]
    elif u.path == "/watch":
        vid = parse_qs(u.query).get("v", [""])[0]
    elif u.path.startswith(("/shorts/", "/live/", "/embed/")):
        vid = u.path.split("/")[2]
    else:
        vid = ""
    ok = len(vid) == 11 and all(c.isalnum() or c in "-_" for c in vid)
    return vid if ok else None


@dataclass
class Media:
    video_id: str
    title: str
    duration: float | None
    stream_url: str
    headers: dict[str, str]


# YouTube often asks cloud servers to "confirm you're not a bot". Some player
# clients are challenged less, so try them in turn. YTDLP_COOKIES (a Netscape
# cookies.txt body) or YTDLP_PROXY (e.g. a residential proxy) lift the block.
CLIENTS = [None, "tv", "web_embedded", "android_vr", "mweb", "web_safari", "ios"]


def _cookie_file() -> str | None:
    body = os.environ.get("YTDLP_COOKIES", "").strip()
    if not body:
        return None
    path = Path(tempfile.gettempdir()) / "blink-yt-cookies.txt"
    if not path.exists():
        path.write_text(body + "\n")
        path.chmod(0o600)
    return str(path)


def _resolve(video_id: str) -> Media:
    import yt_dlp

    base: dict = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if proxy := os.environ.get("YTDLP_PROXY"):
        base["proxy"] = proxy
    if cookies := _cookie_file():
        base["cookiefile"] = cookies
    first_error: Exception | None = None
    for client in CLIENTS:
        opts = dict(base)
        if client:
            opts["extractor_args"] = {"youtube": {"player_client": [client]}}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(
                    f"https://www.youtube.com/watch?v={video_id}", download=False
                )
        except Exception as e:  # try the next client
            logger.info("yt-dlp client %s failed: %s", client or "default", e)
            first_error = first_error or e
            continue
        if not info.get("url"):
            continue
        logger.info("Resolved %s with client %s", video_id, client or "default")
        return Media(
            video_id=video_id,
            title=info.get("title") or "YouTube video",
            duration=info.get("duration"),
            stream_url=info["url"],
            headers=info.get("http_headers") or {},
        )
    raise first_error or RuntimeError("No playable audio found")


async def resolve(video_id: str) -> Media:
    return await asyncio.to_thread(_resolve, video_id)


class Player:
    """Decodes the stream from a position and hands out real-time chunks."""

    def __init__(self, media: Media) -> None:
        self.media = media
        self.proc: asyncio.subprocess.Process | None = None
        self.position = 0.0  # seconds of audio handed out
        self.playing = False
        self.ended = False

    async def _open(self, at: float) -> None:
        await self.close()
        headers = "".join(f"{k}: {v}\r\n" for k, v in self.media.headers.items())
        args = ["ffmpeg", "-loglevel", "error", "-nostdin"]
        args += [
            "-reconnect",
            "1",
            "-reconnect_streamed",
            "1",
            "-reconnect_delay_max",
            "5",
        ]
        if headers:
            args += ["-headers", headers]
        if at > 0:
            args += ["-ss", f"{at:.2f}"]
        args += [
            "-i",
            self.media.stream_url,
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(SAMPLE_RATE),
            "-f",
            "s16le",
            "pipe:1",
        ]
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        self.position = at
        self.ended = False

    async def play(self, at: float) -> None:
        """Start or resume; jump if the viewer seeked away from our position."""
        if self.proc is None or abs(at - self.position) > 1.5:
            await self._open(max(0.0, at))
        self.playing = True

    def pause(self) -> None:
        self.playing = False

    async def chunk(self) -> bytes:
        """The next 100 ms: audio while playing, silence while paused."""
        proc = self.proc
        if not self.playing or self.ended or proc is None or proc.stdout is None:
            return bytes(CHUNK_BYTES)
        try:
            data = await proc.stdout.readexactly(CHUNK_BYTES)
        except asyncio.IncompleteReadError as e:
            data = e.partial + bytes(CHUNK_BYTES - len(e.partial))
            if proc is self.proc:  # not just replaced by a seek
                self.ended = True
        if proc is self.proc:
            self.position += CHUNK_S
        return data

    async def close(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self.proc = None
