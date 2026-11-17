from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest

from app.config import Settings


@pytest.fixture
def settings() -> Settings:
    """Settings for unit tests: ignores the developer's .env file."""
    return Settings(_env_file=None, groq_api_key="test-key", log_json=False)  # type: ignore[call-arg]


class FakeClock:
    """Manual clock. sleep() advances time instantly and records each wait."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def completion(
    text: str, prompt_tokens: int = 10, completion_tokens: int = 5, finish_reason: str = "stop"
) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=text), finish_reason=finish_reason)
        ],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def rate_limit_error(retry_after: str | None = None) -> openai.RateLimitError:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(
        429, headers=headers, request=httpx.Request("POST", "https://api.groq.test/chat")
    )
    return openai.RateLimitError("rate limited", response=response, body=None)


def connection_error() -> openai.APIConnectionError:
    return openai.APIConnectionError(request=httpx.Request("POST", "https://api.groq.test/chat"))


class FakeOpenAI:
    """Stands in for AsyncOpenAI. Each call pops the next scripted result or exception."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("FakeOpenAI called more times than scripted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def fake_openai() -> Callable[[list[Any]], FakeOpenAI]:
    return FakeOpenAI
