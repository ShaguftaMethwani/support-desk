"""Integration tests against a real Postgres with pgvector and migrations applied.

Skipped when no database is reachable, unless REQUIRE_DB=1 (set in CI), which turns
an unreachable database into a failure.
"""

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.config import Settings
from app.db.base import Base
from app.db.models import CorpusChunk, CorpusSource, LlmCache
from app.llm.cache import CachedResponse, PostgresCache

pytestmark = pytest.mark.db


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(Settings().database_url)
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await eng.dispose()
        if os.environ.get("REQUIRE_DB") == "1":
            pytest.fail(f"Database required but unreachable: {type(exc).__name__}")
        pytest.skip("No database reachable")
    yield eng
    await eng.dispose()


async def test_migrated_schema_matches_models(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        ext = await conn.scalar(text("SELECT extname FROM pg_extension WHERE extname='vector'"))
    assert set(Base.metadata.tables) <= tables
    assert ext == "vector"


async def test_postgres_cache_round_trip(engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    cache = PostgresCache(sessions)
    key = uuid.uuid4().hex + uuid.uuid4().hex
    try:
        assert await cache.get(key) is None
        await cache.set(key, "m", CachedResponse("hello", 7, 3))
        await cache.set(key, "m", CachedResponse("ignored", 1, 1))  # duplicate is a no-op
        assert await cache.get(key) == CachedResponse("hello", 7, 3)
    finally:
        async with sessions() as s:
            row = await s.get(LlmCache, key)
            if row is not None:
                await s.delete(row)
                await s.commit()


async def test_full_text_and_vector_columns_work(engine: AsyncEngine) -> None:
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as s:
        source = CorpusSource(
            url=f"https://example.test/{uuid.uuid4()}", publisher="amfi", doc_type="test"
        )
        s.add(source)
        await s.flush()
        s.add(
            CorpusChunk(
                source_id=source.id,
                chunk_index=0,
                text="An exit load is charged on early redemption.",
                embedding=[0.1] * 384,
            )
        )
        await s.flush()
        hit = await s.scalar(
            select(CorpusChunk.id).where(
                CorpusChunk.tsv.op("@@")(text("plainto_tsquery('english', 'exit load')"))
            )
        )
        nearest = await s.scalar(
            select(CorpusChunk.id).order_by(CorpusChunk.embedding.cosine_distance([0.1] * 384))
        )
        await s.rollback()
    assert hit is not None
    assert nearest is not None
