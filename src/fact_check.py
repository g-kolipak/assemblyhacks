"""Low-latency claim extraction and verification with Parallel Decisions.

Pipeline for each finished utterance:
  1. One LLM call pulls out checkable factual claims.
  2. One Parallel Decisions call (a web-grounded System One model) estimates the
     probability that each claim is true. All claims share a single request.
  3. For claims judged false or misleading, one LLM call per claim (run
     concurrently) writes the right answer from the Decisions citations.

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
from typing import Any, Literal, Protocol

logger = logging.getLogger("agent.factcheck")

Verdict = Literal["true", "false", "misleading", "unverified"]

# Utterances shorter than this rarely contain a checkable claim; skipping them
# saves an LLM round trip on "yeah", "okay so", etc.
MIN_WORDS = 5


class ChatFn(Protocol):
    async def __call__(self, system: str, user: str) -> str: ...


class DecideFn(Protocol):
    """Raw Parallel Decisions call: returns the JSON response body."""

    async def __call__(
        self, state: dict[str, str], questions: dict[str, dict[str, Any]]
    ) -> dict[str, Any]: ...


@dataclass
class Thresholds:
    """Map a Decisions probability (0-1) to a verdict."""

    true_at: float = 0.75  # p >= this -> true
    false_at: float = 0.25  # p <= this -> false
    misleading_at: float = 0.45  # false_at < p < this -> misleading

    def verdict(self, p: float) -> Verdict:
        if p >= self.true_at:
            return "true"
        if p <= self.false_at:
            return "false"
        if p < self.misleading_at:
            return "misleading"
        return "unverified"


@dataclass
class CheckResult:
    claim: str
    verdict: Verdict
    probability: float | None = None
    correction: str = ""
    source: str = ""
    url: str = ""
    # How many claims were checked together; >1 means the spoken verdict names
    # its claim so the listener knows which one it refers to.
    batch_size: int = 1
    timings: dict[str, float] = field(default_factory=dict)

    @property
    def needs_flag(self) -> bool:
        return self.verdict in ("false", "misleading")

    def spoken(self) -> str:
        """Short sentence meant to be read aloud."""
        prefix = f"{self.claim.rstrip('.')}: " if self.batch_size > 1 else ""
        source = f" Source: {self.source}." if self.source else ""
        if self.verdict == "true":
            return f"{prefix}That checks out.{source}"
        if self.verdict == "unverified":
            return f"{prefix}I couldn't verify that one."
        label = "That's false" if self.verdict == "false" else "That's misleading"
        if self.correction:
            return f"{prefix}{label}. Actually, {self.correction.rstrip('.')}.{source}"
        return f"{prefix}{label}, but I couldn't find the right answer.{source}"


EXTRACT_SYSTEM = """\
You extract checkable factual claims from live speech transcripts. Today is {today}.
A checkable claim is a specific statement about the world that public sources
could confirm or refute: numbers, dates, records, who did what, scientific facts,
quotes, rankings. Ignore opinions, predictions, jokes, questions, greetings and
vague statements. Rewrite each claim so it stands alone (resolve pronouns using
the context). Return JSON only, no prose:
{{"claims":["standalone claim", "..."]}}
Return at most {max_claims} claims. If there are none, return {{"claims":[]}}."""

EXTRACT_LIVE_SYSTEM = """\
You watch a LIVE speech transcript word by word and extract checkable factual
claims the moment they are fully stated. Today is {today}. A checkable claim is
a specific statement about the world that public sources could confirm or
refute: numbers, dates, records, who did what, scientific facts, rankings.
Ignore opinions, predictions, jokes, questions and vague statements.
The transcript is still being spoken and ends mid-stream at <<live>>. Only
extract a claim when it is COMPLETE. If the words just before <<live>> could
still change its meaning (a number that may continue, e.g. "six thousand" ->
"six thousand eight hundred", a missing unit, object or qualifier), wait.
Never repeat a claim from the already-checked list, even reworded.
Rewrite each claim so it stands alone. Return JSON only:
{{"claims":["standalone claim", "..."]}}
Return at most {max_claims} claims, or {{"claims":[]}} if none are complete."""

CORRECTION_SYSTEM = """\
A fact checker found this claim to be wrong. Today is {today}. Using ONLY the
source excerpts provided, state the right answer. Return JSON only:
{{"correction":"the accurate fact in under 15 words, starting lowercase unless it
  begins with a name",
  "source":"publisher name of the excerpt you used","url":"its exact url"}}
Write for speech: no markdown, no URLs in the sentence, numbers a voice can read,
e.g. "the Eiffel Tower is about 330 metres tall". If the excerpts do not state
the right answer, return {{"correction":"","source":"","url":""}}."""


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


def _host(url: str) -> str:
    match = re.match(r"https?://(?:www\.)?([^/]+)", url)
    return match.group(1) if match else url


def source_name(url: str) -> str:
    """Speakable publisher name: en.wikipedia.org -> Wikipedia."""
    parts = _host(url).split(".")
    if len(parts) >= 3 and len(parts[-2]) <= 3:  # bbc.co.uk, abc.net.au
        return parts[-3].capitalize()
    return (parts[-2] if len(parts) >= 2 else parts[0]).capitalize()


_STOPWORDS = set(
    "a an the is are was were be been of in on at to for and or that this it its "
    "about by with as from has have had than".split()
)


def _terms(text: str) -> set[str]:
    return {w for w in _normalize(text).split() if w not in _STOPWORDS and len(w) > 2}


def rank_citations(claim: str, citations: list[dict]) -> list[dict]:
    """Citations most relevant to one claim first; drop ones sharing no terms.

    Decisions returns one citation list for the whole call, so with several
    claims batched together each claim must pick its own sources.
    """
    terms = _terms(claim)
    scored = []
    for i, c in enumerate(citations):
        text = f"{c.get('title') or ''} {' '.join(c.get('excerpts') or [])}"
        overlap = len(terms & _terms(text))
        if overlap:
            scored.append((-overlap, i, c))
    return [c for _, _, c in sorted(scored, key=lambda t: t[:2])]


def format_citations(citations: list[dict], max_sources: int = 4) -> str:
    blocks = []
    for c in citations[:max_sources]:
        name = c.get("title") or _host(c.get("url", ""))
        excerpt = " ".join(c.get("excerpts") or [])[:700]
        blocks.append(f"{name} ({c.get('url')})\n{excerpt}")
    return "\n\n".join(blocks)


class FactChecker:
    def __init__(
        self,
        chat: ChatFn,
        decide: DecideFn,
        *,
        max_claims: int = 3,
        context_turns: int = 3,
        thresholds: Thresholds | None = None,
    ) -> None:
        self._chat = chat
        self._decide = decide
        self.max_claims = max_claims
        self.thresholds = thresholds or Thresholds()
        self._context: list[str] = []
        self._context_turns = context_turns
        self._seen: set[str] = set()
        self._seen_terms: list[set[str]] = []
        self.checked: list[str] = []

    def is_duplicate(self, claim: str) -> bool:
        """Exact or reworded repeat of a claim already sent for checking."""
        if _normalize(claim) in self._seen:
            return True
        terms = _terms(claim)
        for seen in self._seen_terms:
            union = terms | seen
            if union and len(terms & seen) / len(union) >= 0.6:
                return True
        return False

    def _accept(self, raw_claims: list) -> list[str]:
        claims = []
        for item in raw_claims[: self.max_claims]:
            text = item.get("claim", "") if isinstance(item, dict) else item
            text = str(text).strip()
            if not text or self.is_duplicate(text):
                continue
            self._seen.add(_normalize(text))
            self._seen_terms.append(_terms(text))
            self.checked.append(text)
            claims.append(text)
        return claims

    async def extract_live(self, window: str) -> list[str]:
        """Claims fully stated so far in a live transcript window.

        Called repeatedly as words stream in; returns only claims that have
        not been sent for checking yet.
        """
        if len(window.split()) < 4:
            return []
        already = "\n".join(f"- {c}" for c in self.checked[-12:]) or "(none)"
        raw = await self._chat(
            EXTRACT_LIVE_SYSTEM.format(today=_today(), max_claims=self.max_claims),
            f"Already checked:\n{already}\n\nLive transcript:\n{window} <<live>>",
        )
        try:
            return self._accept(parse_json(raw).get("claims", []))
        except (json.JSONDecodeError, ValueError, AttributeError):
            logger.warning("Could not parse live extraction: %r", raw[:200])
            return []

    async def extract_claims(self, utterance: str) -> list[str]:
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
        return self._accept(data.get("claims", []))

    async def _correct(self, claim: str, citations: list[dict]) -> dict[str, str]:
        if not citations:
            return {}
        raw = await self._chat(
            CORRECTION_SYSTEM.format(today=_today()),
            f"Wrong claim: {claim}\n\nSource excerpts:\n{format_citations(citations)}",
        )
        try:
            return {k: str(v).strip() for k, v in parse_json(raw).items()}
        except (json.JSONDecodeError, ValueError, AttributeError):
            logger.warning("Could not parse correction: %r", raw[:200])
            return {}

    async def check_claims(
        self,
        claims: list[str],
        on_result: Callable[[CheckResult], Awaitable[None] | None] | None = None,
    ) -> list[CheckResult]:
        """Check all claims in one Decisions call; correct the false ones."""
        t0 = time.perf_counter()
        questions = {
            f"claim_{i}": {
                "type": "noul",
                "instructions": f"Is this claim factually accurate? {claim}",
            }
            for i, claim in enumerate(claims)
        }
        try:
            response = await self._decide({"checked_on": _today()}, questions)
        except Exception:
            logger.exception("Parallel Decisions failed")
            response = {}
        t_decide = time.perf_counter() - t0
        answers = response.get("answers", {})
        citations = response.get("citations", [])

        async def finish(i: int, claim: str) -> CheckResult:
            answer = answers.get(f"claim_{i}", {"type": "refusal"})
            result = CheckResult(
                claim,
                "unverified",
                batch_size=len(claims),
                timings={"decide": t_decide},
            )
            if answer.get("type") == "noul":
                p = float(answer["noul"])
                result.probability = p
                result.verdict = self.thresholds.verdict(p)
            relevant = rank_citations(claim, citations)
            if result.verdict == "true" and relevant:
                result.url = relevant[0].get("url", "")
                result.source = source_name(result.url)
            if result.needs_flag:
                t1 = time.perf_counter()
                fix = await self._correct(claim, relevant)
                result.correction = fix.get("correction", "")
                result.url = fix.get("url", "")
                result.source = (
                    source_name(result.url) if result.url else fix.get("source", "")
                )
                result.timings["correction"] = time.perf_counter() - t1
            if on_result is not None:
                maybe = on_result(result)
                if asyncio.iscoroutine(maybe):
                    await maybe
            return result

        return list(await asyncio.gather(*(finish(i, c) for i, c in enumerate(claims))))

    async def process(
        self,
        utterance: str,
        on_result: Callable[[CheckResult], Awaitable[None] | None] | None = None,
    ) -> list[CheckResult]:
        """Extract claims from an utterance and check them.

        `on_result` fires as each claim finishes, so true claims are reported
        right after the Decisions call without waiting for corrections.
        """
        claims = await self.extract_claims(utterance)
        if not claims:
            return []
        return await self.check_claims(claims, on_result)


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
            max_tokens=300,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return res.choices[0].message.content or ""

    return chat
