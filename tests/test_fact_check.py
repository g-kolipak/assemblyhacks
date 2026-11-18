"""Offline tests for the fact-check pipeline using fake LLM and search."""

import asyncio
import json

from fact_check import Claim, FactChecker, parse_json


def make_checker(extract_reply, verdict_reply, search_delay=0.0, calls=None):
    calls = calls if calls is not None else []

    async def chat(system: str, user: str) -> str:
        if "extract" in system:
            return extract_reply
        return verdict_reply(user) if callable(verdict_reply) else verdict_reply

    async def search(objective: str, queries: list[str]) -> str:
        calls.append(queries)
        await asyncio.sleep(search_delay)
        return "[1] Example (https://example.com)\nThe Eiffel Tower is 330 m tall."

    return FactChecker(chat, search), calls


def test_parse_json_handles_code_fences():
    assert parse_json('```json\n{"claims": []}\n```') == {"claims": []}


async def test_false_claim_is_flagged():
    extract = json.dumps(
        {
            "claims": [
                {
                    "claim": "The Eiffel Tower is 500 m tall",
                    "queries": ["Eiffel Tower height"],
                }
            ]
        }
    )
    verdict = json.dumps(
        {
            "verdict": "false",
            "correction": "It is about 330 metres tall",
            "source": "Example",
            "url": "https://example.com",
        }
    )
    checker, calls = make_checker(extract, verdict)
    results = await checker.process(
        "I read that the Eiffel Tower is five hundred metres tall"
    )
    assert len(results) == 1
    r = results[0]
    assert r.verdict == "false" and r.needs_flag
    assert "Actually, It is about 330 metres tall." in r.spoken()
    assert "330" in r.spoken() and "Example" in r.spoken()
    assert calls == [["Eiffel Tower height"]]


async def test_short_utterances_and_duplicates_skip_work():
    extract = json.dumps(
        {"claims": [{"claim": "Water boils at 100 C", "queries": ["boiling point"]}]}
    )
    checker, calls = make_checker(extract, '{"verdict":"true"}')
    assert await checker.process("yeah okay") == []
    await checker.process("water boils at one hundred degrees at sea level")
    await checker.process("again, water boils at one hundred degrees at sea level")
    assert len(calls) == 1


async def test_claims_are_checked_concurrently():
    extract = json.dumps(
        {"claims": [{"claim": f"claim {i}", "queries": [f"q{i}"]} for i in range(3)]}
    )
    checker, _ = make_checker(extract, '{"verdict":"true"}', search_delay=0.3)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    results = await checker.process(
        "three separate factual claims in one sentence here"
    )
    assert len(results) == 3
    assert loop.time() - t0 < 0.6  # sequential would take >= 0.9s


async def test_bad_llm_output_degrades_to_unverified():
    checker, _ = make_checker("not json", "also not json")
    assert await checker.process("some sentence with enough words in it") == []
    result = await checker.check_claim(Claim("x", ["x"]))
    assert result.verdict == "unverified" and not result.needs_flag


async def test_fact_check_agent_builds(monkeypatch):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "test")
    monkeypatch.setenv("PARALLEL_API_KEY", "test")
    import fact_check_agent

    agent = fact_check_agent.FactCheckAgent()
    assert agent.checker.max_claims == 3
    await agent.parallel.aclose()
