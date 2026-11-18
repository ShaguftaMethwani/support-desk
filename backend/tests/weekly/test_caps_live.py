"""Pulse and explainer stay within their caps on 3 real model runs.

Real Groq calls with the response cache off, so each run is a fresh generation.
Run with: uv run pytest -m llm tests/weekly/test_caps_live.py
"""

from datetime import date

import pytest

from app.config import Settings
from app.llm import LLMClient
from app.policy import PolicyContext
from app.policy.text import count_words
from app.policy.validators import EXPLAINER_MAX_BULLETS, PULSE_MAX_WORDS
from app.weekly.fees import BY_KEY
from app.weekly.generate import (
    check_explainer,
    check_pulse,
    explainer_context,
    generate_validated,
    write_explainer,
    write_pulse,
)
from tests.weekly.conftest import EXIT_LOAD_CHUNKS
from tests.weekly.test_generate import DOMAINS, TODAY, _pulse_input

pytestmark = pytest.mark.llm
RUNS = 3


@pytest.fixture
def live_llm() -> LLMClient:
    settings = Settings()
    if settings.groq_api_key is None:
        pytest.skip("GROQ_API_KEY not set")
    return LLMClient(settings, cache=None)


async def test_pulse_stays_within_caps(live_llm: LLMClient) -> None:
    inp = _pulse_input()
    ctx = PolicyContext(DOMAINS, TODAY)
    for _ in range(RUNS):
        result = await generate_validated(
            "weekly pulse", lambda fb: write_pulse(live_llm, inp, fb), lambda t: check_pulse(t, ctx)
        )
        assert 0 < count_words(result.value) <= PULSE_MAX_WORDS
        assert result.value.count("\n1. ") == result.value.count("\n3. ") == 1


async def test_explainer_stays_within_caps(live_llm: LLMClient) -> None:
    ctx = explainer_context(EXIT_LOAD_CHUNKS, DOMAINS, date(2026, 11, 18))
    fee = BY_KEY["exit_load"]
    for _ in range(RUNS):
        result = await generate_validated(
            "fee explainer",
            lambda fb: write_explainer(live_llm, fee, EXIT_LOAD_CHUNKS, fb),
            lambda d: check_explainer(d, ctx),
        )
        draft = result.value
        assert 0 < len(draft.bullets) <= EXPLAINER_MAX_BULLETS
        assert len(set(draft.source_urls)) == 2 and draft.last_checked is not None
