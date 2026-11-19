"""Prepare the eval database. Run by the harness as a subprocess, with DATABASE_URL set to the
eval database, so the app's settings never point at the developer's data.

    python seed.py [--reseed] [--fresh] [--import-cache] --reviews-csv PATH

- Every run: empties every table except the fixtures (corpus sources and chunks), the Groq
  cache and alembic's version table, then loads the eval reviews CSV (with embeddings).
- --reseed: drops and rebuilds the schema (migrations, then the corpus from the local cache in
  backend/corpus/raw and backend/corpus/manual). The Groq cache is kept across it.
- --fresh: also empties the Groq cache, so every model call is real.
- --import-cache: copies cached Groq replies from the database in EVAL_IMPORT_CACHE_URL (the
  dev one), so prompts already answered there cost nothing. Only the cache table is read. The
  URL comes from the environment because it holds a password.
"""

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import asyncpg  # noqa: E402

FIXTURE_TABLES = {"corpus_source", "corpus_chunk", "llm_cache", "alembic_version"}


def plain_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://").replace("?ssl=require", "")


async def keep_cache(conn: asyncpg.Connection) -> None:
    await conn.execute("CREATE SCHEMA IF NOT EXISTS eval_keep")
    exists = await conn.fetchval("SELECT to_regclass('public.llm_cache') IS NOT NULL")
    if exists:
        await conn.execute("DROP TABLE IF EXISTS eval_keep.llm_cache")
        await conn.execute("CREATE TABLE eval_keep.llm_cache AS SELECT * FROM public.llm_cache")


async def restore_cache(conn: asyncpg.Connection) -> int:
    if not await conn.fetchval("SELECT to_regclass('eval_keep.llm_cache') IS NOT NULL"):
        return 0
    status = await conn.execute(
        "INSERT INTO public.llm_cache SELECT * FROM eval_keep.llm_cache ON CONFLICT DO NOTHING"
    )
    return int(status.split()[-1])


async def reseed(url: str) -> None:
    conn = await asyncpg.connect(plain_url(url))
    try:
        await keep_cache(conn)
        # Drop what the migrations made, but keep the schema and the vector extension: creating
        # an extension needs a superuser, which the eval role is not.
        tables = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        if tables:
            names = ", ".join(f'public."{t["tablename"]}"' for t in tables)
            await conn.execute(f"DROP TABLE {names} CASCADE")
        types = await conn.fetch(
            "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace "
            "WHERE n.nspname = 'public' AND t.typtype = 'e'"
        )
        for t in types:
            await conn.execute(f'DROP TYPE public."{t["typname"]}" CASCADE')
    finally:
        await conn.close()
    env = {**os.environ, "DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=BACKEND, env=env,  # noqa: ASYNC221
                   check=True)  # fmt: skip
    subprocess.run([sys.executable, "-m", "corpus.ingest"], cwd=BACKEND, env=env, check=True)  # noqa: ASYNC221
    conn = await asyncpg.connect(plain_url(url))
    try:
        print(f"restored {await restore_cache(conn)} cached Groq replies")
    finally:
        await conn.close()


async def empty_tables(url: str, *, fresh: bool) -> None:
    conn = await asyncpg.connect(plain_url(url))
    try:
        tables = [
            r["tablename"]
            for r in await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public'")
        ]
        keep = FIXTURE_TABLES - ({"llm_cache"} if fresh else set())
        doomed = [t for t in tables if t not in keep]
        if doomed:
            await conn.execute(f"TRUNCATE {', '.join(doomed)} RESTART IDENTITY CASCADE")
        chunks = await conn.fetchval("SELECT count(*) FROM corpus_chunk")
        cached = await conn.fetchval("SELECT count(*) FROM llm_cache")
        print(f"emptied {len(doomed)} tables; corpus chunks {chunks}; cached Groq replies {cached}")
    finally:
        await conn.close()


async def import_cache(url: str, source_url: str) -> None:
    src = await asyncpg.connect(
        plain_url(source_url), ssl="require" if "neon" in source_url else None
    )
    try:
        rows = await src.fetch(
            "SELECT key, model, response_text, prompt_tokens, completion_tokens, created_at "
            "FROM llm_cache"
        )
    finally:
        await src.close()
    dst = await asyncpg.connect(plain_url(url))
    try:
        await dst.executemany(
            "INSERT INTO llm_cache (key, model, response_text, prompt_tokens, completion_tokens,"
            " created_at) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT DO NOTHING",
            [tuple(r) for r in rows],
        )
    finally:
        await dst.close()
    print(f"imported up to {len(rows)} cached Groq replies")


async def load_reviews(csv_path: Path) -> None:
    from app.config import get_settings
    from app.corpus.embed import FastEmbedder
    from app.db.session import get_engine, get_sessionmaker
    from app.weekly.reviews import ingest_reviews, load_csv

    rows = load_csv(csv_path)
    dates = [r.date for r in rows]
    async with get_sessionmaker()() as s:
        stats = await ingest_reviews(
            s, rows, FastEmbedder(),
            window_start=min(dates), window_end=max(dates),
            dedupe_similarity=get_settings().review_dedupe_similarity,
        )  # fmt: skip
        await s.commit()
    await get_engine().dispose()
    counts = {k: v for k, v in stats.as_dict().items() if k != "review_ids"}
    print(f"reviews {date.isoformat(min(dates))} to {max(dates)}: {counts}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reseed", action="store_true")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--import-cache", action="store_true")
    parser.add_argument("--reviews-csv", type=Path, required=True)
    args = parser.parse_args()
    url = os.environ["DATABASE_URL"]
    if args.reseed:
        await reseed(url)
    await empty_tables(url, fresh=args.fresh)
    if args.import_cache and not args.fresh:
        await import_cache(url, os.environ["EVAL_IMPORT_CACHE_URL"])
    await load_reviews(args.reviews_csv)


if __name__ == "__main__":
    asyncio.run(main())
