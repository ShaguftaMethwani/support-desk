"""Response cache for LLM calls, keyed by a hash of the model, prompt and parameters.

Repeated questions and repeated eval runs cost no Groq quota. Model inputs are always
post-redaction, so cached text never contains raw PII.
"""

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import LlmCache
from app.observability import get_logger

log = get_logger(__name__)


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
    """Postgres-backed cache with an in-memory copy in front.

    - get: memory first, then Postgres (the copy keeps repeats in this process instant).
    - set: first write wins, in memory as in Postgres (ON CONFLICT DO NOTHING).
    - write_behind: the Postgres write runs in the background instead of delaying the reply.
      A lost write only costs a later cache miss. Call flush() to wait for pending writes.
    """

    MEMORY_LIMIT = 2000

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, write_behind: bool = False
    ) -> None:
        self._session_factory = session_factory
        self._write_behind = write_behind
        self._memory: dict[str, CachedResponse] = {}
        self._pending: set[asyncio.Task[None]] = set()

    def _remember(self, key: str, value: CachedResponse) -> None:
        if key in self._memory:
            return
        if len(self._memory) >= self.MEMORY_LIMIT:
            self._memory.pop(next(iter(self._memory)))  # oldest first
        self._memory[key] = value

    async def get(self, key: str) -> CachedResponse | None:
        if key in self._memory:
            return self._memory[key]
        async with self._session_factory() as session:
            row = await session.scalar(select(LlmCache).where(LlmCache.key == key))
            if row is None:
                return None
            value = CachedResponse(row.response_text, row.prompt_tokens, row.completion_tokens)
        self._remember(key, value)
        return value

    async def set(self, key: str, model: str, value: CachedResponse) -> None:
        self._remember(key, value)
        if not self._write_behind:
            await self._write(key, model, value)
            return
        task = asyncio.create_task(self._write(key, model, value))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def flush(self) -> None:
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)

    async def _write(self, key: str, model: str, value: CachedResponse) -> None:
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
        try:
            async with self._session_factory() as session:
                await session.execute(stmt)
                await session.commit()
        except Exception:  # noqa: BLE001 - a failed cache write only means a later miss
            if not self._write_behind:
                raise
            log.warning("llm_cache_write_failed", model=model)
