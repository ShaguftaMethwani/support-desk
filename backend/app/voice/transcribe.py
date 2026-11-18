"""Groq Whisper transcription (architecture section 11).

The audio goes to Groq straight from memory. The five scheme names and the fee terms go in
Whisper's `prompt`, which biases it towards spelling them correctly. Only timings and sizes
are logged, never the transcript.
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

import openai
from openai import AsyncOpenAI

from app.config import Settings
from app.corpus.schemes import SCHEMES
from app.llm.ratelimit import TokenBucket
from app.observability import get_logger

log = get_logger(__name__)

FEE_TERMS = (
    "exit load", "expense ratio", "TER", "SIP", "NAV", "lock-in", "riskometer", "benchmark",
    "stamp duty", "STT", "Direct Plan", "Regular Plan", "redemption", "INDmoney",
)  # fmt: skip
WHISPER_PROMPT = (
    "Questions about HDFC mutual funds: "
    + ", ".join(s.name for s in SCHEMES)
    + ". Terms: "
    + ", ".join(FEE_TERMS)
    + "."
)
RETRIES = 2


class TranscriptionError(Exception):
    """Whisper could not transcribe the clip. The caller answers 503."""


@dataclass(frozen=True)
class Transcription:
    text: str  # raw: holds whatever was said, so it goes straight to the guard
    duration: float | None  # seconds, as Whisper measured the clip


class Transcriber(Protocol):
    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> Transcription: ...


class GroqTranscriber:
    def __init__(self, settings: Settings, client: AsyncOpenAI | None = None) -> None:
        if client is None:
            if settings.groq_api_key is None:
                raise TranscriptionError("GROQ_API_KEY is not set")
            client = AsyncOpenAI(
                api_key=settings.groq_api_key.get_secret_value(),
                base_url=settings.groq_base_url,
                timeout=settings.whisper_timeout_seconds,
                max_retries=0,
            )
        self._client = client
        self._model = settings.whisper_model
        self._requests = TokenBucket(settings.whisper_rpm, 60.0)
        self._lock = asyncio.Lock()

    async def _wait_for_slot(self) -> None:
        async with self._lock:
            wait = self._requests.wait_time(1)
            if wait > 0:
                await asyncio.sleep(wait)
            self._requests.take(1)

    async def transcribe(self, audio: bytes, filename: str, content_type: str) -> Transcription:
        started = time.monotonic()
        for attempt in range(RETRIES + 1):
            await self._wait_for_slot()
            try:
                result = await self._client.audio.transcriptions.create(
                    file=(filename, audio, content_type),
                    model=self._model,
                    prompt=WHISPER_PROMPT,
                    language="en",
                    temperature=0.0,
                    response_format="verbose_json",
                )
                break
            except (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError):
                if attempt == RETRIES:
                    raise TranscriptionError("transcription is temporarily unavailable") from None
                await asyncio.sleep(2.0 * (attempt + 1))
            except openai.APIStatusError as exc:
                # 400 and similar: the clip itself was refused (format, empty audio).
                raise TranscriptionError(
                    f"the clip could not be transcribed ({exc.status_code})"
                ) from None
        duration = getattr(result, "duration", None)
        latency_ms = int((time.monotonic() - started) * 1000)
        log.info("voice_transcribed", model=self._model, bytes=len(audio), duration_s=duration,
                 latency_ms=latency_ms)  # fmt: skip
        return Transcription(str(result.text or ""), float(duration) if duration else None)
