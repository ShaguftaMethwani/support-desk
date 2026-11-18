"""The desk's production dependencies, built once on first use (loads the embedding model)."""

from functools import lru_cache

from app.orchestrator.desk import DeskDeps


@lru_cache
def get_desk_deps() -> DeskDeps:
    from app.config import get_settings
    from app.corpus.embed import FastEmbedder
    from app.corpus.retrieve import Retriever
    from app.db.session import get_sessionmaker
    from app.llm import LLMClient
    from app.llm.cache import PostgresCache

    settings = get_settings()
    sessions = get_sessionmaker()
    embedder = FastEmbedder()
    return DeskDeps(
        sessions=sessions,
        llm=LLMClient(settings, PostgresCache(sessions, write_behind=True)),
        embedder=embedder,
        retriever_for=lambda session: Retriever(session, embedder),
        settings=settings,
    )
