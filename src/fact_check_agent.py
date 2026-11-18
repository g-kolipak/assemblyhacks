"""Live fact-checker: listens to streaming audio and flags false claims.

AssemblyAI streams speech-to-text and decides when each utterance ends. Every
finished utterance goes through `FactChecker` in the background, so listening
never pauses while claims are verified with Parallel Search. The agent only
speaks when a claim is false or misleading; every verdict is logged.

    uv run python src/fact_check_agent.py console
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path

import assemblyai_tts
from dotenv import dotenv_values, load_dotenv
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    StopResponse,
    TurnHandlingOptions,
    cli,
    llm,
)
from livekit.plugins import assemblyai, silero

from fact_check import CheckResult, FactChecker, gateway_chat
from parallel_api import ParallelAPI, ParallelConfig

load_dotenv(".env.local")

logger = logging.getLogger("agent")
logger.setLevel(logging.INFO)

defaults = dotenv_values(Path(__file__).with_name("gateway.env"))
LLM_MODEL = os.environ.get("LLM_MODEL", defaults["LLM_MODEL"])
STT_MODEL = os.environ.get("ASSEMBLYAI_STT_MODEL", "universal-3-6-pro")
TTS_VOICE = os.environ.get("ASSEMBLYAI_TTS_VOICE", "aai-kyla")
# Set FACTCHECK_SPEAK=0 to only log verdicts (e.g. while monitoring a podcast).
SPEAK = os.environ.get("FACTCHECK_SPEAK", "1") != "0"
GREETING = os.environ.get(
    "AGENT_GREETING",
    "I'm listening. If I hear a false claim, I'll give you the right answer.",
)

ICONS = {"true": "✓", "false": "✗", "misleading": "!", "unverified": "?"}


def build_checker() -> tuple[FactChecker, ParallelAPI]:
    parallel = ParallelAPI(
        # Turbo search with a small excerpt budget keeps the verdict LLM fast.
        ParallelConfig(
            search_mode=os.environ.get("PARALLEL_SEARCH_MODE", "turbo"),  # type: ignore[arg-type]
            max_chars_total=int(os.environ.get("PARALLEL_MAX_CHARS", "3000")),
            max_results=int(os.environ.get("PARALLEL_MAX_RESULTS", "4")),
        )
    )
    checker = FactChecker(gateway_chat(LLM_MODEL), parallel.search)
    return checker, parallel


class FactCheckAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions="You are a silent live fact checker.")
        self.checker, self.parallel = build_checker()
        self._tasks: set[asyncio.Task] = set()

    async def on_enter(self) -> None:
        self.session.say(GREETING)

    async def on_exit(self) -> None:
        await self.parallel.aclose()

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage
    ) -> None:
        text = (new_message.text_content or "").strip()
        if text:
            logger.info("HEARD · %s", text)
            task = asyncio.create_task(self._check(text, time.perf_counter()))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        # Never generate a conversational reply; keep listening.
        raise StopResponse()

    async def _check(self, text: str, started: float) -> None:
        async def report(result: CheckResult) -> None:
            elapsed = time.perf_counter() - started
            logger.info(
                "%s %s · %s%s%s · %.1fs (search %.1fs, verdict %.1fs)",
                ICONS[result.verdict],
                result.verdict.upper(),
                result.claim,
                f" → {result.correction}" if result.correction else "",
                f" [{result.url}]" if result.url else "",
                elapsed,
                result.timings.get("search", 0),
                result.timings.get("verdict", 0),
            )
            if SPEAK and result.needs_flag:
                self.session.say(
                    result.spoken(), add_to_chat_ctx=False, allow_interruptions=True
                )

        try:
            await self.checker.process(text, on_result=report)
        except Exception:
            logger.exception("Fact check failed for %r", text)


server = AgentServer()


@server.rtc_session(agent_name="parallel-fact-checker")
async def entrypoint(ctx: JobContext) -> None:
    if not os.getenv("PARALLEL_API_KEY"):
        raise RuntimeError(
            "The fact checker calls Parallel Search directly. Set PARALLEL_API_KEY "
            "in .env.local (https://platform.parallel.ai)."
        )
    session = AgentSession(
        # AssemblyAI streaming STT also decides when each utterance has ended.
        stt=assemblyai.STT(model=STT_MODEL),
        tts=assemblyai_tts.TTS(voice_id=TTS_VOICE, language="english"),
        vad=silero.VAD.load(),
        turn_handling=TurnHandlingOptions(
            turn_detection="stt",
            interruption={"mode": "vad"},
        ),
        expressive=False,
    )
    await session.start(agent=FactCheckAgent(), room=ctx.room)
    await ctx.connect()


if __name__ == "__main__":
    cli.run_app(server)
