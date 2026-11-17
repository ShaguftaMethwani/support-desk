"""Real Groq calls. Skipped in CI. Run with: uv run pytest -m llm"""

import pytest
from pydantic import BaseModel

from app.config import Settings
from app.llm import LLMClient
from app.llm.cache import InMemoryCache

pytestmark = pytest.mark.llm


@pytest.fixture
def live_settings() -> Settings:
    s = Settings()
    if s.groq_api_key is None:
        pytest.skip("GROQ_API_KEY not set")
    return s


async def test_small_model_call_and_cache(live_settings: Settings) -> None:
    client = LLMClient(live_settings, InMemoryCache())
    msgs = [{"role": "user", "content": "Reply with the single word: ready"}]
    first = await client.complete("intent", msgs, max_tokens=100)
    second = await client.complete("intent", msgs, max_tokens=100)
    assert first.text.strip()
    assert not first.cached and second.cached
    assert first.prompt_tokens > 0


class Answer(BaseModel):
    word: str


async def test_json_schema_call(live_settings: Settings) -> None:
    client = LLMClient(live_settings, InMemoryCache())
    msgs = [{"role": "user", "content": 'Return JSON exactly like {"word": "ready"}'}]
    result = await client.complete("intent", msgs, schema=Answer, max_tokens=150)
    assert isinstance(result.parsed, Answer)
