"""Audio from a YouTube link, as 16 kHz mono PCM, paced like live playback.

yt-dlp resolves the link to a direct audio stream; ffmpeg decodes it. The
website plays the same video in an embed and reports play, pause and seeks,
so the fact-checker hears what the viewer is watching. Only YouTube links are
accepted, so the server cannot be pointed at arbitrary (internal) URLs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
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


def _resolve(video_id: str) -> Media:
    import yt_dlp

    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(
            f"https://www.youtube.com/watch?v={video_id}", download=False
        )
    return Media(
        video_id=video_id,
        title=info.get("title") or "YouTube video",
        duration=info.get("duration"),
        stream_url=info["url"],
        headers=info.get("http_headers") or {},
    )


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
