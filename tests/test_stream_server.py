"""Offline tests for the streaming backend's transcript segmentation."""

from fact_check import CheckResult
from stream_server import TurnSegmenter, build_app, origin_allowed, result_event


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


def test_end_of_turn_emits_transcript_once():
    seg = TurnSegmenter()
    assert seg.feed(turn(0, "Everest is six thousand", final=False)) == []
    text = "Everest is six thousand metres tall."
    assert seg.feed(turn(0, text, end=True, transcript=text)) == [text]
    # A second (formatted) end-of-turn message for the same turn is ignored.
    assert seg.feed(turn(0, text, end=True, transcript=text)) == []


def test_long_monologue_flushes_at_sentence_boundary():
    seg = TurnSegmenter()
    first = "The Moon is about three hundred eighty four thousand km away."
    talk = first + " And the Sun is"
    assert seg.feed(turn(1, talk)) == [first]
    # The rest is emitted at end of turn without repeating the first sentence.
    rest = talk + " a star."
    assert seg.feed(turn(1, rest, end=True, transcript=rest)) == [
        "And the Sun is a star."
    ]


def test_partial_sentence_flushes_once_speaker_moves_on():
    seg = TurnSegmenter()
    # Sentence end is the last (revisable) word: wait.
    assert seg.feed(turn(2, "The Great Wall is visible from space.", final=False)) == []
    # More words follow it, so the sentence is done; check it now.
    msg = turn(2, "The Great Wall is visible from space. Anyway, the", final=False)
    assert seg.feed(msg) == ["The Great Wall is visible from space."]
    # Too-short sentences wait to be grouped with the next one.
    assert seg.feed(turn(3, "So yes. And then", final=False)) == []


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
                text = "Mount Everest is about six thousand metres tall."
                await ws.send_json(turn(0, text, end=True, transcript=text))
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
        if "extract" in system:
            return json.dumps({"claims": ["Mount Everest is 6000 m tall"]})
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
    assert kinds[:2] == ["status", "sentence"] and kinds[-1] == "verdict"
    verdict = events[-1]
    assert verdict["verdict"] == "false"
    assert verdict["speak"] == (
        "That's false. Actually, Everest is 8,849 metres tall. Source: Wiki."
    )
    assert got_audio == [1600]
