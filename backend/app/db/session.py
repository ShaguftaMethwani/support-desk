from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import get_settings


@lru_cache
def get_engine() -> AsyncEngine:
    # Modest pool. Each queue item being drafted can hold up to 3 connections (its own and its
    # agents'), and up to `desk_parallel_items` items run at once. Neon's pooled endpoint
    # accepts far more client connections than this.
    # hide_parameters keeps bound values (user text) out of SQLAlchemy error messages and logs.
    return create_async_engine(
        get_settings().database_url,
        pool_size=10,
        max_overflow=10,
        pool_pre_ping=True,
        hide_parameters=True,
    )


@lru_cache
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with get_sessionmaker()() as session:
        yield session
