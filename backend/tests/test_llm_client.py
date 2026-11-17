from collections.abc import Callable
from typing import Any

import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs

from app.config import Settings
from app.llm import LLMClient, LLMNotConfiguredError, LLMOutputError, LLMUnavailableError
from app.llm.cache import InMemoryCache
from app.llm.tasks import UnknownTaskError
from tests.conftest import (
    FakeClock,
    FakeOpenAI,
    completion,
    connection_error,
    rate_limit_error,
)

MSGS = [{"role": "user", "content": "What is the exit load on HDFC Liquid Fund?"}]


def make_client(
    settings: Settings, fake: FakeOpenAI, clock: FakeClock, cache: InMemoryCache | None = None
) -> LLMClient:
    return LLMClient(settings, cache, fake, clock=clock, sleep=clock.sleep)  # type: ignore[arg-type]


FakeFactory = Callable[[list[Any]], FakeOpenAI]


async def test_routes_tasks_to_tier_models(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    fake = fake_openai([completion("a"), completion("b")])
    client = make_client(settings, fake, clock)
    small = await client.complete("intent", MSGS)
    large = await client.complete("orchestrate", MSGS)
    assert fake.calls[0]["model"] == "openai/gpt-oss-20b"
    assert fake.calls[1]["model"] == "openai/gpt-oss-120b"
    assert fake.calls[0]["reasoning_effort"] == "low"
    assert (small.text, large.text) == ("a", "b")
    assert small.prompt_tokens == 10 and small.completion_tokens == 5 and small.total_tokens == 15


async def test_cache_serves_repeat_calls(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    cache = InMemoryCache()
    fake = fake_openai([completion("answer")])
    client = make_client(settings, fake, clock, cache)
    first = await client.complete("facts", MSGS)
    second = await client.complete("facts", MSGS)
    assert len(fake.calls) == 1
    assert not first.cached and second.cached
    assert second.text == "answer" and second.total_tokens == 15


async def test_cache_key_depends_on_parameters(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    fake = fake_openai([completion("a"), completion("b")])
    client = make_client(settings, fake, clock, InMemoryCache())
    await client.complete("facts", MSGS, max_tokens=100)
    await client.complete("facts", MSGS, max_tokens=200)
    assert len(fake.calls) == 2


async def test_cache_disabled_by_setting(fake_openai: FakeFactory, clock: FakeClock) -> None:
    settings = Settings(_env_file=None, groq_api_key="k", llm_cache_enabled=False)  # type: ignore[call-arg]
    fake = fake_openai([completion("a"), completion("a")])
    client = make_client(settings, fake, clock, InMemoryCache())
    await client.complete("intent", MSGS)
    await client.complete("intent", MSGS)
    assert len(fake.calls) == 2


async def test_json_mode_sets_response_format(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    fake = fake_openai([completion("{}"), completion("plain")])
    client = make_client(settings, fake, clock)
    await client.complete("intent", MSGS, json_mode=True)
    await client.complete("intent", [{"role": "user", "content": "other"}])
    assert fake.calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in fake.calls[1]


async def test_retries_rate_limit_using_retry_after(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    fake = fake_openai([rate_limit_error("2"), completion("ok")])
    client = make_client(settings, fake, clock)
    result = await client.complete("intent", MSGS)
    assert result.text == "ok"
    assert clock.sleeps == [2.0]


async def test_gives_up_after_max_retries(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    fake = fake_openai([rate_limit_error() for _ in range(4)])
    client = make_client(settings, fake, clock)
    with pytest.raises(LLMUnavailableError):
        await client.complete("intent", MSGS)
    assert len(fake.calls) == 4  # first try + 3 retries
    assert clock.sleeps == [1.0, 2.0, 4.0]  # exponential backoff without retry-after


async def test_retries_transient_connection_errors(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    fake = fake_openai([connection_error(), completion("ok")])
    client = make_client(settings, fake, clock)
    assert (await client.complete("intent", MSGS)).text == "ok"


class Intent(BaseModel):
    intent: str
    confidence: float


async def test_schema_output_is_parsed(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    fake = fake_openai([completion('{"intent": "factual", "confidence": 0.9}')])
    client = make_client(settings, fake, clock)
    result = await client.complete("intent", MSGS, schema=Intent)
    assert isinstance(result.parsed, Intent) and result.parsed.intent == "factual"
    assert fake.calls[0]["response_format"] == {"type": "json_object"}


async def test_schema_gets_one_repair_try(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    fake = fake_openai(
        [
            completion('{"intent": "factual"}'),
            completion('{"intent": "factual", "confidence": 0.8}'),
        ]
    )
    client = make_client(settings, fake, clock)
    result = await client.complete("intent", MSGS, schema=Intent)
    assert isinstance(result.parsed, Intent)
    assert len(fake.calls) == 2
    assert "did not match the required JSON schema" in fake.calls[1]["messages"][-1]["content"]


async def test_schema_fails_closed_after_repair(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    fake = fake_openai([completion("not json"), completion("still not json")])
    client = make_client(settings, fake, clock)
    with pytest.raises(LLMOutputError):
        await client.complete("intent", MSGS, schema=Intent)


async def test_unknown_task_fails_before_any_call(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    fake = fake_openai([])
    client = make_client(settings, fake, clock)
    with pytest.raises(UnknownTaskError):
        await client.complete("write_a_poem", MSGS)
    assert fake.calls == []


async def test_reasoning_effort_omitted_when_unset(
    fake_openai: FakeFactory, clock: FakeClock
) -> None:
    settings = Settings(_env_file=None, groq_api_key="k", reasoning_effort=None)  # type: ignore[call-arg]
    fake = fake_openai([completion("a")])
    await make_client(settings, fake, clock).complete("intent", MSGS)
    assert "reasoning_effort" not in fake.calls[0]


async def test_small_token_budgets_are_raised_to_the_floor(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    fake = fake_openai([completion("a")])
    await make_client(settings, fake, clock).complete("intent", MSGS, max_tokens=5)
    assert fake.calls[0]["max_tokens"] == 64


async def test_empty_reply_fails_closed_and_is_not_cached(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    cache = InMemoryCache()
    fake = fake_openai([completion("", finish_reason="length"), completion("ok")])
    client = make_client(settings, fake, clock, cache)
    with pytest.raises(LLMOutputError):
        await client.complete("intent", MSGS)
    assert len(cache) == 0
    assert (await client.complete("intent", MSGS)).text == "ok"


def test_missing_api_key_is_reported() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    with pytest.raises(LLMNotConfiguredError):
        LLMClient(settings)


async def test_logs_never_contain_prompt_or_response_text(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    fake = fake_openai([rate_limit_error("1"), completion("Exit load is 0.007%")])
    client = make_client(settings, fake, clock)
    with capture_logs() as logs:
        await client.complete("facts", MSGS)
    assert logs, "expected llm log events"
    dumped = repr(logs)
    assert "exit load" not in dumped.lower()
    assert "HDFC" not in dumped
