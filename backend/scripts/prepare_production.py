"""Clean a copy of the dev database for production: keep the corpus, the reviews and the Groq
cache; empty everything else (queue items, drafts, actions, weekly runs, explainers, users).

Run from backend/, with DATABASE_URL set to the production branch (not the one in .env):

    DATABASE_URL='postgresql://...' uv run python -m scripts.prepare_production --dry-run
    DATABASE_URL='postgresql://...' uv run python -m scripts.prepare_production --confirm <host>

<host> is the first part of the database host name (for Neon, the "ep-..." endpoint id). It
must match, so the dev database can't be emptied by mistake. Then create the logins with
scripts.create_user, using the same DATABASE_URL.
"""

import argparse
import asyncio
import os
import sys
from urllib.parse import urlsplit

import asyncpg

KEEP = ("corpus_source", "corpus_chunk", "review", "llm_cache", "alembic_version")


def plain(url: str) -> str:
    """asyncpg takes postgresql:// and sslmode; drop SQLAlchemy's driver and channel_binding."""
    parts = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://"))
    query = "&".join(
        p.replace("ssl=", "sslmode=", 1) if p.startswith("ssl=") else p
        for p in parts.query.split("&")
        if p and not p.startswith("channel_binding")
    )
    return parts._replace(query=query).geturl()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--confirm", help="the first part of the database host name")
    parser.add_argument("--dry-run", action="store_true", help="only show what would be emptied")
    args = parser.parse_args()
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL to the production database (it is not read from .env).")
    host = urlsplit(url.replace("postgresql+asyncpg://", "postgresql://")).hostname or ""
    label = host.split(".")[0]
    print(f"Database host: {host}")
    if not args.dry_run and args.confirm != label:
        sys.exit(f"Refusing: pass --confirm {label} to empty this database.")

    conn = await asyncpg.connect(plain(url))
    try:
        tables = [r["tablename"] for r in await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
        )]  # fmt: skip
        for t in tables:
            n = await conn.fetchval(f'SELECT count(*) FROM "{t}"')
            print(f"  {'keep ' if t in KEEP else 'empty'}  {t:22} {n:>7} rows")
        doomed = [t for t in tables if t not in KEEP]
        if args.dry_run:
            print("Dry run: nothing changed.")
            return 0
        if doomed:
            await conn.execute(f"TRUNCATE {', '.join(doomed)} RESTART IDENTITY CASCADE")
        print(f"Emptied {len(doomed)} tables. Now create the logins with scripts.create_user.")
    finally:
        await conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
