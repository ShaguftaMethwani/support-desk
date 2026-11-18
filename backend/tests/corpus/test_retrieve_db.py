"""Retrieval against real Postgres, with the hash embedder (no model download)."""

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.config import Settings
from app.corpus.embed import HashEmbedder
from app.corpus.retrieve import Retriever, score
from app.db.models import CorpusChunk, CorpusSource

CHUNKS = [
    (
        "hdfc_liquid",
        "[HDFC Liquid Fund] Exit load is 0.0070% if redeemed on day 1 after allotment.",
    ),
    ("hdfc_flexi_cap", "[HDFC Flexi Cap Fund] Exit load is 1% if redeemed within 30 days."),
    ("general", "The riskometer has six levels from Low to Very High risk."),
    ("general", "ELSS has a lock-in period of 3 years from the date of allotment."),
]


def test_score_boosts_strong_keyword_matches() -> None:
    assert score(0.70, 1) > score(0.70, 8) > score(0.70, None) == 0.70
    assert score(0.80, None) > score(0.70, 1)  # a boost never beats a clearly closer chunk


@pytest.fixture
async def session() -> AsyncIterator[AsyncSession]:
    engine: AsyncEngine = create_async_engine(Settings().database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        if os.environ.get("REQUIRE_DB") == "1":
            pytest.fail(f"Database required but unreachable: {type(exc).__name__}")
        pytest.skip("No database reachable")
    async with AsyncSession(engine, expire_on_commit=False) as s:
        source = CorpusSource(
            key=f"test_{uuid.uuid4().hex[:8]}",
            url=f"https://www.sebi.gov.in/{uuid.uuid4()}",
            publisher="sebi",
            doc_type="faq",
            fetched_at=datetime(2026, 11, 1, tzinfo=UTC),
        )
        s.add(source)
        await s.flush()
        embedder = HashEmbedder()
        vectors = embedder.embed_documents([t for _, t in CHUNKS])
        s.add_all(
            CorpusChunk(source_id=source.id, chunk_index=i, text=t, scheme=sc, embedding=v)
            for i, ((sc, t), v) in enumerate(zip(CHUNKS, vectors, strict=True))
        )
        await s.flush()
        yield s
        await s.rollback()
    await engine.dispose()


pytestmark = pytest.mark.db


async def test_scheme_filter_keeps_scheme_and_general_chunks(session: AsyncSession) -> None:
    retriever = Retriever(session, HashEmbedder())
    results, trace = await retriever.search("exit load", scheme="hdfc_liquid", k=10)
    test_rows = [r for r in results if r.source_key.startswith("test_")]
    assert {r.scheme for r in test_rows} <= {"hdfc_liquid", "general"}
    assert test_rows[0].text.startswith("[HDFC Liquid Fund]")
    assert trace.scheme_filter == "hdfc_liquid" and trace.top


async def test_results_are_sorted_by_score(session: AsyncSession) -> None:
    results, _ = await Retriever(session, HashEmbedder()).search("ELSS lock-in period", k=4)
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)
    assert any("lock-in period of 3 years" in r.text for r in results)
