"""Response cache for LLM calls, keyed by a hash of the model, prompt and parameters.

Repeated questions and repeated eval runs cost no Groq quota. Model inputs are always
post-redaction, so cached text never contains raw PII.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import LlmCache


@dataclass(frozen=True)
class CachedResponse:
    text: str
    prompt_tokens: int
    completion_tokens: int


class ResponseCache(Protocol):
    async def get(self, key: str) -> CachedResponse | None: ...

    async def set(self, key: str, model: str, value: CachedResponse) -> None: ...


def cache_key(
    model: str,
    messages: Sequence[Mapping[str, str]],
    *,
    json_mode: bool,
    temperature: float,
    max_tokens: int,
    reasoning_effort: str | None = None,
) -> str:
    payload = {
        "reasoning_effort": reasoning_effort,
        "model": model,
        "messages": [dict(m) for m in messages],
        "json_mode": json_mode,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class InMemoryCache:
    """Process-local cache, used in tests and as a fallback without a database."""

    def __init__(self) -> None:
        self._store: dict[str, CachedResponse] = {}

    async def get(self, key: str) -> CachedResponse | None:
        return self._store.get(key)

    async def set(self, key: str, model: str, value: CachedResponse) -> None:
        self._store[key] = value

    def __len__(self) -> int:
        return len(self._store)


class PostgresCache:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, key: str) -> CachedResponse | None:
        async with self._session_factory() as session:
            row = await session.scalar(select(LlmCache).where(LlmCache.key == key))
            if row is None:
                return None
            return CachedResponse(row.response_text, row.prompt_tokens, row.completion_tokens)

    async def set(self, key: str, model: str, value: CachedResponse) -> None:
        stmt = (
            insert(LlmCache)
            .values(
                key=key,
                model=model,
                response_text=value.text,
                prompt_tokens=value.prompt_tokens,
                completion_tokens=value.completion_tokens,
            )
            .on_conflict_do_nothing(index_elements=[LlmCache.key])
        )
        async with self._session_factory() as session:
            await session.execute(stmt)
            await session.commit()
