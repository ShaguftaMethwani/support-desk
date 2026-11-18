from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest

from app.agents.facts import FactsAgent, numbers_in, unsupported_numbers
from app.config import Settings
from app.corpus.retrieve import RetrievedChunk, SearchTrace
from app.llm import LLMClient
from tests.conftest import FakeClock, FakeOpenAI, completion

LIQUID = "[HDFC Liquid Fund]\nExit load | Day 1: 0.0070% | Day 7 onwards: Nil"
SID = "https://portal.amfiindia.com/spages/sai9.pdf"


def chunk(text: str = LIQUID, similarity: float = 0.82, idx: int = 1) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=f"c{idx}",
        source_key="hdfc_liquid_kim",
        url=SID,
        title="HDFC Liquid Fund KIM",
        publisher="hdfc_mf",
        doc_type="kim",
        scheme="hdfc_liquid",
        section="Exit load",
        page=3,
        text=text,
        fetched_at=datetime(2026, 11, 10, tzinfo=UTC),
        similarity=similarity,
        text_rank=1,
        score=similarity,
    )


class FakeRetriever:
    def __init__(self, chunks: list[RetrievedChunk]) -> None:
        self.chunks = chunks
        self.calls: list[dict[str, Any]] = []

    async def search(
        self, query: str, *, scheme: str | None = None, include_platform: bool = False, k: int = 4
    ) -> tuple[list[RetrievedChunk], SearchTrace]:
        self.calls.append({"query": query, "scheme": scheme, "k": k})
        top = [
            {"source": c.source_key, "chunk_id": c.chunk_id, "similarity": c.similarity}
            for c in self.chunks
        ]
        return self.chunks, SearchTrace(query, scheme, include_platform, top)


def agent(
    settings: Settings, fake: FakeOpenAI, clock: FakeClock, chunks: list[RetrievedChunk]
) -> tuple[FactsAgent, FakeRetriever]:
    retriever = FakeRetriever(chunks)
    llm = LLMClient(settings, None, fake, clock=clock, sleep=clock.sleep)  # type: ignore[arg-type]
    return FactsAgent(retriever, llm, min_similarity=0.65), retriever


FakeFactory = Callable[[list[Any]], FakeOpenAI]


async def test_answers_with_one_citation(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    fake = fake_openai(
        [
            completion(
                '{"not_covered": false, "answer": "Exit load is 0.0070% on day 1.", "passage": 1}'
            )
        ]
    )
    facts, retriever = agent(settings, fake, clock, [chunk()])
    result = await facts.run("Exit load on HDFC Liquid Fund?", scheme="hdfc_liquid")
    assert result.status == "answered"
    assert result.answer == "Exit load is 0.0070% on day 1."
    assert result.citation and result.citation.url == SID and result.citation.page == 3
    assert result.citation.last_checked == date(2026, 11, 10)
    assert retriever.calls[0]["scheme"] == "hdfc_liquid"
    assert fake.calls[0]["model"] == settings.model_large
    assert "Passages:\n[1] Source: HDFC Liquid Fund KIM" in fake.calls[0]["messages"][1]["content"]


async def test_low_similarity_is_not_covered_without_a_model_call(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock
) -> None:
    fake = fake_openai([])
    facts, _ = agent(settings, fake, clock, [chunk(similarity=0.42)])
    result = await facts.run("What is the capital of France?")
    assert result.status == "not_covered" and result.reason == "low_similarity"
    assert result.searched and "searched" in result.searched_summary
    assert fake.calls == []


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ('{"not_covered": true}', "model_not_covered"),
        ('{"not_covered": false, "answer": "Exit load is 0.0070%.", "passage": 7}', "bad_passage"),
        (
            '{"not_covered": false, "answer": "Exit load is 0.5% on day 1.", "passage": 1}',
            "number_mismatch",
        ),
        ('{"not_covered": false, "answer": "  ", "passage": 1}', "model_not_covered"),
    ],
)
async def test_not_covered_cases(
    settings: Settings, fake_openai: FakeFactory, clock: FakeClock, reply: str, reason: str
) -> None:
    facts, _ = agent(settings, fake_openai([completion(reply)]), clock, [chunk()])
    result = await facts.run("Exit load on HDFC Liquid Fund?")
    assert result.status == "not_covered" and result.reason == reason
    assert result.citation is None and result.searched


def test_number_extraction() -> None:
    assert numbers_in("Rs. 1,500 or 0.0070% on day 1") == {1500.0, 0.007, 1.0}
    assert unsupported_numbers("Lock-in is 5 years.", "lock-in period of 3 years") == {5.0}
    assert unsupported_numbers("Sent by the 15th.", "by 15th of succeeding month") == set()
