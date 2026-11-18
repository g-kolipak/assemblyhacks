"""Offline tests for the fact-check pipeline using fake LLM and Decisions."""

import asyncio
import json

from fact_check import FactChecker, Thresholds, parse_json

CITATIONS = [
    {
        "url": "https://en.wikipedia.org/wiki/Eiffel_Tower",
        "title": "Eiffel Tower - Wikipedia",
        "excerpts": ["The tower is 330 metres tall."],
    }
]
CORRECTION = json.dumps(
    {
        "correction": "the Eiffel Tower is about 330 metres tall",
        "source": "Wikipedia",
        "url": "https://en.wikipedia.org/wiki/Eiffel_Tower",
    }
)


def make_checker(claims, probabilities, *, correction=CORRECTION, decide_delay=0.0):
    calls = {"decide": [], "correct": 0}

    async def chat(system: str, user: str) -> str:
        if "extract" in system:
            return claims if isinstance(claims, str) else json.dumps({"claims": claims})
        calls["correct"] += 1
        await asyncio.sleep(decide_delay)
        return correction

    async def decide(state, questions):
        calls["decide"].append(questions)
        await asyncio.sleep(decide_delay)
        answers = {}
        for i, key in enumerate(questions):
            p = probabilities[i]
            answers[key] = (
                {"type": "refusal"} if p is None else {"type": "noul", "noul": p}
            )
        return {"mode": "fast", "answers": answers, "citations": CITATIONS}

    return FactChecker(chat, decide), calls


def test_parse_json_handles_code_fences():
    assert parse_json('```json\n{"claims": []}\n```') == {"claims": []}


def test_thresholds():
    t = Thresholds()
    assert [t.verdict(p) for p in (0.9, 0.1, 0.35, 0.6)] == [
        "true",
        "false",
        "misleading",
        "unverified",
    ]


async def test_false_claim_gets_right_answer_spoken():
    checker, calls = make_checker(["The Eiffel Tower is 500 metres tall"], [0.02])
    [r] = await checker.process("I read that the Eiffel Tower is 500 metres tall")
    assert r.verdict == "false" and r.needs_flag and r.probability == 0.02
    assert r.spoken() == (
        "That's false. Actually, the Eiffel Tower is about 330 metres tall. "
        "Source: Wikipedia."
    )
    q = calls["decide"][0]["claim_0"]
    assert q["type"] == "noul" and "500 metres" in q["instructions"]


async def test_true_claim_is_confirmed_without_correction_call():
    checker, calls = make_checker(["The Eiffel Tower is in Paris"], [0.98])
    [r] = await checker.process("the Eiffel Tower is in Paris France you know")
    assert r.verdict == "true"
    assert r.spoken() == "That checks out. Source: Wikipedia."
    assert calls["correct"] == 0


async def test_all_claims_share_one_decisions_call():
    claims = ["Eiffel Tower is tall", "Eiffel Tower is short", "Eiffel Tower is old"]
    checker, calls = make_checker(claims, [0.9, 0.1, None])
    results = await checker.process("three separate factual claims in one sentence")
    assert len(calls["decide"]) == 1
    assert set(calls["decide"][0]) == {"claim_0", "claim_1", "claim_2"}
    assert [r.verdict for r in results] == ["true", "false", "unverified"]
    # With several claims, each spoken verdict names its claim.
    assert (
        results[0].spoken()
        == "Eiffel Tower is tall: That checks out. Source: Wikipedia."
    )
    assert results[2].spoken() == "Eiffel Tower is old: I couldn't verify that one."


async def test_true_verdict_reported_before_slow_correction():
    checker, _ = make_checker(["A is true", "B is false"], [0.9, 0.1], decide_delay=0.2)
    order = []
    await checker.process("two claims in this one sentence here", order.append)
    assert [r.verdict for r in order] == ["true", "false"]


async def test_short_utterances_and_duplicates_skip_work():
    checker, calls = make_checker(["Water boils at 100 C"], [0.95])
    assert await checker.process("yeah okay") == []
    await checker.process("water boils at one hundred degrees at sea level")
    await checker.process("again, water boils at one hundred degrees at sea level")
    assert len(calls["decide"]) == 1


async def test_bad_output_degrades_gracefully():
    checker, _ = make_checker("not json", [])
    assert await checker.process("some sentence with enough words in it") == []

    checker, _ = make_checker(["X is Y"], [0.05], correction="nope")
    [r] = await checker.process("x is y and that is a fact")
    assert r.verdict == "false"
    assert r.spoken() == "That's false, but I couldn't find the right answer."


async def test_decisions_failure_is_unverified():
    async def chat(system, user):
        return json.dumps({"claims": ["X is Y"]})

    async def decide(state, questions):
        raise RuntimeError("network down")

    [r] = await FactChecker(chat, decide).process("x is y and that is a fact")
    assert r.verdict == "unverified"


async def test_fact_check_agent_builds(monkeypatch):
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "test")
    monkeypatch.setenv("PARALLEL_API_KEY", "test")
    import fact_check_agent

    agent = fact_check_agent.FactCheckAgent()
    assert agent.checker.max_claims == 3
    await agent.parallel.aclose()


def test_sources_are_matched_per_claim():
    from fact_check import rank_citations, source_name

    cites = [
        {"url": "https://www.britannica.com/everest", "title": "Mount Everest height"},
        {
            "url": "https://en.wikipedia.org/wiki/Eiffel",
            "excerpts": ["Eiffel Tower, Paris"],
        },
    ]
    assert rank_citations("The Eiffel Tower is in Paris", cites)[0] is cites[1]
    assert rank_citations("Water boils at 100 C", cites) == []
    assert source_name("https://en.wikipedia.org/x") == "Wikipedia"
    assert source_name("https://www.britannica.com/x") == "Britannica"
    assert source_name("https://www.bbc.co.uk/news") == "Bbc"
