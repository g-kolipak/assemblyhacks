"""Offline tests for the streaming backend's transcript segmentation."""

from fact_check import CheckResult
from stream_server import (
    ClaimWatcher,
    LiveWindow,
    build_app,
    origin_allowed,
    result_event,
)


def turn(order, words, *, final=True, end=False, transcript=None):
    msg = {
        "type": "Turn",
        "turn_order": order,
        "end_of_turn": end,
        "words": [{"text": w, "word_is_final": final} for w in words.split()],
    }
    if transcript is not None:
        msg["transcript"] = transcript
    return msg


def test_window_emits_every_step_words():
    win = LiveWindow(step=2, size=45)
    assert win.feed(turn(0, "Mount", final=False)) is None
    assert win.feed(turn(0, "Mount Everest", final=False)) == "Mount Everest"
    assert win.feed(turn(0, "Mount Everest is", final=False)) is None
    assert win.feed(turn(0, "Mount Everest is about", final=False)) == (
        "Mount Everest is about"
    )


def test_window_keeps_history_and_ignores_formatted_copy():
    win = LiveWindow(step=2, size=6)
    text = "Everest is 6,000 meters tall."
    assert (
        win.feed(
            turn(0, "everest is six thousand meters tall", end=True, transcript=text)
        )
        == text
    )
    assert win.feed(turn(0, text, end=True, transcript=text)) is None
    # Next turn: the window keeps the newest `size` words across turns.
    assert win.feed(turn(1, "And the Great", final=False)) == (
        "6,000 meters tall. And the Great"
    )


async def test_watcher_coalesces_to_latest_window():
    import asyncio

    seen_windows, fired = [], []

    class FakeChecker:
        async def extract_live(self, window):
            seen_windows.append(window)
            await asyncio.sleep(0.05)
            return [f"claim from {window}"] if "tall" in window else []

    watcher = ClaimWatcher(FakeChecker(), lambda claims, t: fired.extend(claims))
    for w in ["a b", "a b c d", "a b c d e f", "a b c d e f tall"]:
        watcher.update(w)
        await asyncio.sleep(0)
    await watcher.drain()
    # First call ran at once; the two stale windows were skipped.
    assert seen_windows == ["a b", "a b c d e f tall"]
    assert fired == ["claim from a b c d e f tall"]


def test_origin_check(monkeypatch):
    monkeypatch.delenv("FACTCHECK_ALLOWED_ORIGIN", raising=False)
    assert origin_allowed("chrome-extension://abcdef")
    assert origin_allowed(None)
    assert not origin_allowed("https://evil.example")
    monkeypatch.setenv("FACTCHECK_ALLOWED_ORIGIN", "chrome-extension://mine")
    assert not origin_allowed("chrome-extension://other")


def test_result_event_shape():
    r = CheckResult("X is Y", "false", 0.1, "X is Z", "Wiki", "https://w.org")
    e = result_event(r, "A", heard_at=0.0)
    assert e["type"] == "verdict" and e["speak"].startswith("That's false.")
    assert e["speaker"] == "A" and e["latency_ms"] >= 0


def test_app_routes():
    paths = {r.resource.canonical for r in build_app().router.routes()}
    assert {"/stream", "/health"} <= paths


async def test_stream_end_to_end_with_fakes(monkeypatch):
    """Audio in -> fake AssemblyAI turns -> fake checker -> verdict out."""
    import json

    import aiohttp
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    import stream_server

    got_audio = []

    async def fake_aai(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "Begin"})
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.BINARY:
                got_audio.append(len(msg.data))
                words = "Mount Everest is about six thousand metres tall".split()
                for n in range(1, len(words) + 1):
                    await ws.send_json(turn(0, " ".join(words[:n]), final=True))
            elif json.loads(msg.data).get("type") == "Terminate":
                await ws.send_json({"type": "Termination"})
                break
        return ws

    aai_app = web.Application()
    aai_app.router.add_get("/v3/ws", fake_aai)

    class FakeParallel:
        async def decide_raw(self, state, questions):
            return {
                "answers": {k: {"type": "noul", "noul": 0.03} for k in questions},
                "citations": [{"url": "https://w.org", "title": "Mount Everest"}],
            }

        async def aclose(self):
            pass

    async def chat(system, user):
        if "LIVE" in system:
            # Only report the claim once its last word has arrived.
            if "metres tall <<live>>" in user:
                return json.dumps({"claims": ["Mount Everest is 6000 m tall"]})
            return json.dumps({"claims": []})
        return json.dumps(
            {"correction": "Everest is 8,849 metres tall", "source": "Wiki"}
        )

    async with TestServer(aai_app) as aai_server:
        monkeypatch.setattr(
            stream_server, "assemblyai_url", lambda: str(aai_server.make_url("/v3/ws"))
        )
        monkeypatch.setattr(stream_server, "ParallelAPI", FakeParallel)
        monkeypatch.setattr(stream_server, "gateway_chat", lambda model: chat)
        monkeypatch.setenv("ASSEMBLYAI_API_KEY", "test")
        async with TestServer(stream_server.build_app()) as server:
            async with aiohttp.ClientSession() as http:
                ws = await http.ws_connect(
                    server.make_url("/stream"),
                    headers={"Origin": "chrome-extension://test"},
                )
                await ws.send_bytes(b"\x00\x00" * 800)
                events = []
                async for msg in ws:
                    events.append(json.loads(msg.data))
                    if events[-1]["type"] == "verdict":
                        await ws.send_str("stop")
                        break
                await ws.close()

    kinds = [e["type"] for e in events]
    assert kinds[0] == "status" and "checking" in kinds and kinds[-1] == "verdict"
    assert kinds.count("checking") == 1  # detected once, not re-checked
    verdict = events[-1]
    assert verdict["verdict"] == "false"
    assert verdict["speak"] == (
        "That's false. Actually, Everest is 8,849 metres tall. Source: Wiki."
    )
    assert got_audio == [1600]
