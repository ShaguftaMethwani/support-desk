"""Runs weekly runs in the background, and resumes interrupted ones when the API starts."""

import asyncio
import uuid
from collections.abc import Callable
from functools import lru_cache

from sqlalchemy import select

from app.config import get_settings
from app.db.models import WeeklyRun
from app.observability import get_logger
from app.weekly.pipeline import WeeklyDeps, execute_run

log = get_logger(__name__)


class WeeklyRunner:
    def __init__(self, deps_factory: Callable[[], WeeklyDeps]) -> None:
        self._deps_factory = deps_factory
        self._deps: WeeklyDeps | None = None
        self._active: set[uuid.UUID] = set()
        self._tasks: set[asyncio.Task[str | None]] = set()

    @property
    def deps(self) -> WeeklyDeps:
        if self._deps is None:  # built on first use: loads the embedding model
            self._deps = self._deps_factory()
        return self._deps

    async def run(self, run_id: uuid.UUID) -> str | None:
        """Execute one run. A run already executing in this process is not started twice."""
        if run_id in self._active:
            return None
        self._active.add(run_id)
        try:
            status = await execute_run(run_id, self.deps)
            log.info("weekly_run_finished", run_id=str(run_id), status=status)
            return status
        except Exception:  # noqa: BLE001 - logged; the run stays in_progress and resumes later
            log.exception("weekly_run_crashed", run_id=str(run_id))
            return None
        finally:
            self._active.discard(run_id)

    def spawn(self, run_id: uuid.UUID) -> asyncio.Task[str | None]:
        task = asyncio.create_task(self.run(run_id))
        self._tasks.add(task)  # keep a reference until it finishes
        task.add_done_callback(self._tasks.discard)
        return task

    async def resume_interrupted(self) -> list[uuid.UUID]:
        """Start every run left in_progress (for example by a restart mid-run)."""
        async with self.deps.sessions() as s:
            ids = list(
                await s.scalars(select(WeeklyRun.id).where(WeeklyRun.status == "in_progress"))
            )
        for run_id in ids:
            log.info("weekly_run_resuming", run_id=str(run_id))
            self.spawn(run_id)
        return ids


def default_deps() -> WeeklyDeps:
    from app.corpus.embed import FastEmbedder
    from app.corpus.retrieve import Retriever
    from app.db.session import get_sessionmaker
    from app.llm import LLMClient
    from app.llm.cache import PostgresCache

    settings = get_settings()
    sessions = get_sessionmaker()
    embedder = FastEmbedder()
    return WeeklyDeps(
        sessions=sessions,
        llm=LLMClient(settings, PostgresCache(sessions, write_behind=True)),
        embedder=embedder,
        retriever_for=lambda session: Retriever(session, embedder),
        settings=settings,
    )


@lru_cache
def get_weekly_runner() -> WeeklyRunner:
    return WeeklyRunner(default_deps)
