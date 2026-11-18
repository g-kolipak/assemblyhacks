"""Low-latency claim extraction and verification.

Pipeline for each finished utterance:
  1. One LLM call pulls out checkable factual claims plus search keywords.
  2. Every claim is checked concurrently: Parallel Search (turbo mode) gathers
     evidence, then one LLM call returns a verdict grounded in that evidence.

This module has no LiveKit dependency so it can be reused from any app and
unit-tested with fakes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Literal, Protocol

logger = logging.getLogger("agent.factcheck")

Verdict = Literal["true", "false", "misleading", "unverified"]
VERDICTS: tuple[Verdict, ...] = ("true", "false", "misleading", "unverified")

# Utterances shorter than this rarely contain a checkable claim; skipping them
# saves an LLM round trip on "yeah", "okay so", etc.
MIN_WORDS = 5


class ChatFn(Protocol):
    async def __call__(self, system: str, user: str) -> str: ...


class SearchFn(Protocol):
    async def __call__(self, objective: str, queries: list[str]) -> str: ...


@dataclass
class Claim:
    text: str
    queries: list[str]


@dataclass
class CheckResult:
    claim: str
    verdict: Verdict
    correction: str = ""
    source: str = ""
    url: str = ""
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def needs_flag(self) -> bool:
        return self.verdict in ("false", "misleading")

    def spoken(self) -> str:
        """Short interjection meant to be read aloud."""
        label = "That's false" if self.verdict == "false" else "That's misleading"
        parts = [f"{label}."]
        if self.correction:
            parts.append(f"Actually, {self.correction.rstrip('.')}.")
        else:
            parts.append("I couldn't find the exact correct figure.")
        if self.source:
            parts.append(f"Source: {self.source}.")
        return " ".join(parts)


EXTRACT_SYSTEM = """\
You extract checkable factual claims from live speech transcripts. Today is {today}.
A checkable claim is a specific statement about the world that public sources
could confirm or refute: numbers, dates, records, who did what, scientific facts,
quotes, rankings. Ignore opinions, predictions, jokes, questions, greetings and
vague statements. Resolve pronouns using the context when possible.
Return JSON only, no prose:
{{"claims":[{{"claim":"standalone claim","queries":["2-6 keyword query"]}}]}}
Return at most {max_claims} claims and one or two queries each. If there are none,
return {{"claims":[]}}."""

VERDICT_SYSTEM = """\
You are a careful fact checker. Today is {today}. Judge the claim ONLY against the
evidence excerpts provided. Verdicts:
- "true": evidence supports the claim.
- "false": evidence clearly contradicts the claim.
- "misleading": partly right but wrong in an important detail (number, date,
  person) or missing key context.
- "unverified": evidence is missing, off-topic or inconclusive.
Return JSON only:
{{"verdict":"true|false|misleading|unverified",
  "correction":"the right answer as one short spoken sentence",
  "source":"publisher name of the best supporting excerpt",
  "url":"its url"}}
The correction is REQUIRED for "false" and "misleading": state the accurate fact
from the evidence directly, e.g. "the Eiffel Tower is about 330 metres tall".
Leave it empty for "true" and "unverified". Write the correction for speech:
no markdown, no URLs, numbers in plain words or digits a voice can read."""


def _today() -> str:
    return date.today().strftime("%A, %B %d, %Y")


def parse_json(raw: str) -> dict:
    """Parse the first JSON object in an LLM reply (tolerates code fences)."""
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            raise
        return json.loads(match.group(0))


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


class FactChecker:
    def __init__(
        self,
        chat: ChatFn,
        search: SearchFn,
        *,
        max_claims: int = 3,
        context_turns: int = 3,
    ) -> None:
        self._chat = chat
        self._search = search
        self.max_claims = max_claims
        self._context: list[str] = []
        self._context_turns = context_turns
        self._seen: set[str] = set()

    async def extract_claims(self, utterance: str) -> list[Claim]:
        context = "\n".join(self._context[-self._context_turns :])
        self._context.append(utterance)
        if len(utterance.split()) < MIN_WORDS:
            return []
        user = f"Earlier context:\n{context or '(none)'}\n\nNew utterance:\n{utterance}"
        raw = await self._chat(
            EXTRACT_SYSTEM.format(today=_today(), max_claims=self.max_claims), user
        )
        try:
            data = parse_json(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Could not parse claim extraction: %r", raw[:200])
            return []
        claims = []
        for item in data.get("claims", [])[: self.max_claims]:
            text = str(item.get("claim", "")).strip()
            queries = [
                str(q).strip() for q in item.get("queries", []) if str(q).strip()
            ]
            key = _normalize(text)
            if not text or key in self._seen:
                continue
            self._seen.add(key)
            claims.append(Claim(text=text, queries=queries[:2] or [text]))
        return claims

    async def check_claim(self, claim: Claim) -> CheckResult:
        t0 = time.perf_counter()
        try:
            evidence = await self._search(
                f"Find authoritative evidence that confirms or refutes: {claim.text}",
                claim.queries,
            )
        except Exception:
            logger.exception("Search failed for %r", claim.text)
            return CheckResult(claim.text, "unverified")
        t_search = time.perf_counter() - t0
        raw = await self._chat(
            VERDICT_SYSTEM.format(today=_today()),
            f"Claim: {claim.text}\n\nEvidence:\n{evidence}",
        )
        t_total = time.perf_counter() - t0
        try:
            data = parse_json(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Could not parse verdict: %r", raw[:200])
            data = {}
        verdict = str(data.get("verdict", "unverified")).lower()
        if verdict not in VERDICTS:
            verdict = "unverified"
        return CheckResult(
            claim=claim.text,
            verdict=verdict,  # type: ignore[arg-type]
            correction=str(data.get("correction", "")).strip(),
            source=str(data.get("source", "")).strip(),
            url=str(data.get("url", "")).strip(),
            timings={"search": t_search, "verdict": t_total - t_search},
        )

    async def process(
        self,
        utterance: str,
        on_result: Callable[[CheckResult], Awaitable[None] | None] | None = None,
    ) -> list[CheckResult]:
        """Extract claims and check them concurrently.

        `on_result` fires as each claim finishes, so the fastest verdict is
        reported without waiting for the slowest one.
        """
        claims = await self.extract_claims(utterance)
        if not claims:
            return []

        async def run(claim: Claim) -> CheckResult:
            result = await self.check_claim(claim)
            if on_result is not None:
                maybe = on_result(result)
                if asyncio.iscoroutine(maybe):
                    await maybe
            return result

        return list(await asyncio.gather(*(run(c) for c in claims)))


def gateway_chat(model: str, api_key: str | None = None) -> ChatFn:
    """Chat function backed by the AssemblyAI LLM gateway (OpenAI-compatible)."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI(
        base_url="https://llm-gateway.assemblyai.com/v1",
        api_key=api_key or os.environ["ASSEMBLYAI_API_KEY"],
    )

    async def chat(system: str, user: str) -> str:
        res = await client.chat.completions.create(
            model=model,
            temperature=0,
            max_tokens=400,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return res.choices[0].message.content or ""

    return chat
