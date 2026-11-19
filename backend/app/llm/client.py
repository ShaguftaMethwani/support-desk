"""Groq adapter.

One entry point, LLMClient.complete(task, messages). It picks the model from the task's tier,
serves repeats from the cache, waits on the rate limiter, retries rate-limit and transient
errors, validates JSON output against a Pydantic schema, and records token counts.
It logs task, model, tokens and timing only, never prompt or response text.

Fallback model: when Groq reports that a model's daily quota is spent (a 429 whose retry-after
is longer than a minute, or that names a per-day limit), that model is skipped until the quota
resets and the call goes to the other tier's model. Callers that need to know (the desk tags
the draft) wrap their work in track_fallback().
"""

import asyncio
import json
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, cast

import openai
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel, ValidationError

from app.config import Settings, Tier
from app.llm.cache import CachedResponse, ResponseCache, cache_key
from app.llm.ratelimit import Clock, RateLimiter, Sleep
from app.llm.tasks import tier_for
from app.observability import get_logger

log = get_logger(__name__)

Messages = Sequence[Mapping[str, str]]

MAX_RETRY_AFTER_SECONDS = 60.0
# Groq's JSON mode answers 400 with this code when the model's reply isn't valid JSON.
JSON_VALIDATE_FAILED = "json_validate_failed"
MAX_JSON_RETRY_TOKENS = 4096


class LLMError(Exception):
    """Base error for LLM calls. Callers fail closed on any of these."""


class LLMNotConfiguredError(LLMError):
    pass


class LLMUnavailableError(LLMError):
    """Retries exhausted. The item should show 'temporarily unavailable'."""


class LLMOutputError(LLMError):
    """The model did not return valid JSON for the schema, even after one repair."""


class LLMQuotaError(LLMUnavailableError):
    """A model's daily quota is spent. Retrying the same model won't help today."""

    def __init__(self, message: str, model: str, retry_after_s: float | None) -> None:
        super().__init__(message)
        self.model = model
        self.retry_after_s = retry_after_s


# Models used as a fallback during the current piece of work (see track_fallback). A list is
# shared by reference, so agents running in child tasks record into their parent's list.
_FALLBACK_USED: ContextVar[list[str] | None] = ContextVar("llm_fallback_used", default=None)


def fallback_models_used() -> list[str]:
    """The fallback models used so far inside the current track_fallback() block."""
    return list(_FALLBACK_USED.get() or [])


@contextmanager
def track_fallback() -> Iterator[list[str]]:
    """Collects the fallback models used by every LLM call made inside the block."""
    used: list[str] = []
    token = _FALLBACK_USED.set(used)
    try:
        yield used
    finally:
        _FALLBACK_USED.reset(token)


@dataclass(frozen=True)
class LLMResult:
    text: str
    task: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cached: bool
    latency_ms: int
    parsed: BaseModel | None = None
    fallback: bool = False  # answered by the other tier's model: the first one's quota ran out

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


def estimate_tokens(messages: Messages) -> int:
    """Rough token estimate (about 4 characters per token) for rate-limit reservations."""
    chars = sum(len(m.get("content", "")) for m in messages)
    return chars // 4 + 4 * len(messages)


def _retry_after_seconds(exc: openai.APIStatusError) -> float | None:
    value = exc.response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return min(max(float(value), 0.0), MAX_RETRY_AFTER_SECONDS)
    except ValueError:
        return None


# A per-minute limit resets within a minute. A longer wait, or a message naming a daily limit,
# means the day's quota is spent.
QUOTA_HINTS = ("per day", "(rpd)", "(tpd)", "requests per day", "tokens per day")
DEFAULT_QUOTA_RESET_SECONDS = 3600.0


def _quota_reset_seconds(exc: openai.RateLimitError) -> float | None:
    """Seconds until the quota resets when this 429 is a spent daily quota, else None."""
    raw = exc.response.headers.get("retry-after")
    try:
        wait = float(raw) if raw is not None else None
    except ValueError:
        wait = None
    message = str(exc).lower()
    if wait is not None and wait > MAX_RETRY_AFTER_SECONDS:
        return wait
    if any(hint in message for hint in QUOTA_HINTS):
        return wait if wait is not None and wait > 0 else DEFAULT_QUOTA_RESET_SECONDS
    return None


def other_tier(tier: Tier) -> Tier:
    return Tier.LARGE if tier is Tier.SMALL else Tier.SMALL


def _backoff_seconds(attempt: int) -> float:
    return float(min(2**attempt, 30))


def _parse(schema: type[BaseModel], text: str) -> BaseModel:
    return schema.model_validate(json.loads(text))


class LLMClient:
    def __init__(
        self,
        settings: Settings,
        cache: ResponseCache | None = None,
        client: AsyncOpenAI | None = None,
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if client is None:
            if settings.groq_api_key is None:
                raise LLMNotConfiguredError("GROQ_API_KEY is not set")
            client = AsyncOpenAI(
                api_key=settings.groq_api_key.get_secret_value(),
                base_url=settings.groq_base_url,
                timeout=settings.llm_timeout_seconds,
                max_retries=0,  # retries are handled here, using Groq's retry-after header
            )
        self._settings = settings
        self._client = client
        self._cache = cache if settings.llm_cache_enabled else None
        self._clock = clock
        self._sleep = sleep
        self._limiters = {
            tier: RateLimiter(*settings.limits_for_tier(tier), clock=clock, sleep=sleep)
            for tier in Tier
        }
        self._exhausted_until: dict[str, float] = {}  # model -> clock time its quota resets

    def _exhausted(self, model: str) -> bool:
        until = self._exhausted_until.get(model)
        return until is not None and self._clock() < until

    async def complete(
        self,
        task: str,
        messages: Messages,
        *,
        schema: type[BaseModel] | None = None,
        json_mode: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> LLMResult:
        """Run one completion for a task. With a schema, JSON is validated with one repair try."""
        json_mode = json_mode or schema is not None
        result = await self._complete_raw(task, messages, json_mode, temperature, max_tokens)
        if schema is None:
            return result

        try:
            return replace(result, parsed=_parse(schema, result.text))
        except (json.JSONDecodeError, ValidationError) as exc:
            log.warning("llm_output_invalid", task=task, model=result.model, attempt=1)
            repair: list[Mapping[str, str]] = [
                *messages,
                {"role": "assistant", "content": result.text},
                {
                    "role": "user",
                    "content": (
                        "Your previous reply did not match the required JSON schema. "
                        f"Error: {str(exc)[:400]}. Reply again with only valid JSON."
                    ),
                },
            ]
            retry = await self._complete_raw(task, repair, json_mode, temperature, max_tokens)
            try:
                return replace(retry, parsed=_parse(schema, retry.text))
            except (json.JSONDecodeError, ValidationError) as exc2:
                log.warning("llm_output_invalid", task=task, model=retry.model, attempt=2)
                raise LLMOutputError(f"Invalid JSON output for task {task!r}") from exc2

    async def _complete_raw(
        self,
        task: str,
        messages: Messages,
        json_mode: bool,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult:
        tier = tier_for(task)
        primary = self._settings.model_for_tier(tier)
        backup_tier = other_tier(tier)
        backup = self._settings.model_for_tier(backup_tier)
        can_fall_back = self._settings.llm_fallback_enabled and backup != primary
        args = (task, messages, json_mode, temperature, max_tokens)
        if not (can_fall_back and self._exhausted(primary)):
            try:
                return await self._call(*args, tier=tier, fallback=False)
            except LLMQuotaError as exc:
                if not can_fall_back:
                    raise
                wait = exc.retry_after_s or DEFAULT_QUOTA_RESET_SECONDS
                self._exhausted_until[primary] = self._clock() + wait
                log.warning("llm_quota_exhausted", task=task, model=primary, fallback=backup,
                            reset_s=int(wait))  # fmt: skip
        if self._exhausted(backup):
            raise LLMUnavailableError(f"daily quota spent for {primary} and {backup}")
        result = await self._call(*args, tier=backup_tier, fallback=True)
        used = _FALLBACK_USED.get()
        if used is not None:
            used.append(result.model)
        return result

    async def _call(
        self,
        task: str,
        messages: Messages,
        json_mode: bool,
        temperature: float,
        max_tokens: int,
        *,
        tier: Tier,
        fallback: bool,
    ) -> LLMResult:
        model = self._settings.model_for_tier(tier)
        effort = self._settings.reasoning_effort
        max_tokens = max(max_tokens, self._settings.min_completion_tokens)
        key = cache_key(
            model,
            messages,
            json_mode=json_mode,
            temperature=temperature,
            max_tokens=max_tokens,
            reasoning_effort=effort,
        )

        if self._cache is not None:
            hit = await self._cache.get(key)
            if hit is not None:
                log.info("llm_call", task=task, model=model, cached=True, tokens=0,
                         fallback=fallback)  # fmt: skip
                return LLMResult(hit.text, task, model, hit.prompt_tokens, hit.completion_tokens,
                                 True, 0, fallback=fallback)  # fmt: skip

        limiter = self._limiters[tier]
        reserved = estimate_tokens(messages) + max_tokens
        extra: dict[str, Any] = {}
        if json_mode:
            extra["response_format"] = {"type": "json_object"}
        if effort is not None:
            extra["reasoning_effort"] = effort
        attempt = 0
        json_retried = False
        while True:
            await limiter.acquire(reserved)
            started = self._clock()
            try:
                response = await self._client.chat.completions.create(
                    model=model,
                    messages=cast(list[ChatCompletionMessageParam], [dict(m) for m in messages]),
                    temperature=temperature,
                    max_tokens=max_tokens,
                    **extra,
                )
                break
            except openai.RateLimitError as exc:
                limiter.settle(reserved, 0)
                reset = _quota_reset_seconds(exc)
                if reset is not None:
                    # No retries: the quota won't come back within this request.
                    raise LLMQuotaError(f"daily quota spent for {model}", model, reset) from exc
                delay = _retry_after_seconds(exc) or _backoff_seconds(attempt)
                reason = "rate_limited"
                error: Exception = exc
            except (openai.APIConnectionError, openai.InternalServerError) as exc:
                limiter.settle(reserved, 0)
                delay = _backoff_seconds(attempt)
                reason = "transient_error"
                error = exc
            except openai.BadRequestError as exc:
                limiter.settle(reserved, 0)
                if getattr(exc, "code", None) != JSON_VALIDATE_FAILED:
                    log.error("llm_unavailable", task=task, model=model, reason="http_400")
                    raise LLMUnavailableError("Groq refused the call (400)") from exc
                # JSON mode: the model's reply wasn't valid JSON, usually because a reasoning
                # model ran out of tokens mid-answer. One more try with twice the budget.
                if json_retried:
                    log.warning(
                        "llm_output_invalid", task=task, model=model, reason=JSON_VALIDATE_FAILED
                    )
                    raise LLMOutputError(f"Groq could not produce JSON for task {task!r}") from exc
                json_retried = True
                max_tokens = min(max_tokens * 2, MAX_JSON_RETRY_TOKENS)
                reserved = estimate_tokens(messages) + max_tokens
                log.warning("llm_json_retry", task=task, model=model, max_tokens=max_tokens)
                continue
            except openai.APIError as exc:
                # Anything else won't get better by retrying: a revoked key (401), a retired
                # model name (404), a rejected request (400). Fail closed at once, so the item
                # shows "temporarily unavailable" instead of the request crashing.
                limiter.settle(reserved, 0)
                status = getattr(exc, "status_code", None)
                log.error("llm_unavailable", task=task, model=model,
                          reason=f"http_{status}" if status else type(exc).__name__)  # fmt: skip
                raise LLMUnavailableError(f"Groq refused the call ({status})") from exc
            if attempt >= self._settings.llm_max_retries:
                log.error("llm_unavailable", task=task, model=model, reason=reason)
                raise LLMUnavailableError(f"{reason} after {attempt} retries") from error
            attempt += 1
            log.warning(
                "llm_retry", task=task, model=model, reason=reason, attempt=attempt, delay_s=delay
            )
            await self._sleep(delay)

        latency_ms = int((self._clock() - started) * 1000)
        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else estimate_tokens(messages)
        completion_tokens = usage.completion_tokens if usage else 0
        limiter.settle(reserved, prompt_tokens + completion_tokens)
        choice = response.choices[0]
        text = choice.message.content or ""
        if not text.strip():
            # Reasoning models can spend the whole budget thinking and return nothing.
            # Never cache an empty reply, and fail closed so the caller can react.
            log.warning(
                "llm_empty_output",
                task=task,
                model=model,
                finish_reason=getattr(choice, "finish_reason", None),
                completion_tokens=completion_tokens,
            )
            raise LLMOutputError(
                f"Empty reply for task {task!r} (finish_reason="
                f"{getattr(choice, 'finish_reason', None)}). Raise max_tokens or lower reasoning."
            )

        if self._cache is not None:
            await self._cache.set(
                key, model, CachedResponse(text, prompt_tokens, completion_tokens)
            )

        log.info(
            "llm_call",
            task=task,
            model=model,
            cached=False,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            retries=attempt,
            fallback=fallback,
        )
        return LLMResult(text, task, model, prompt_tokens, completion_tokens, False, latency_ms,
                         fallback=fallback)  # fmt: skip
