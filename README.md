# The Support Desk

Graduation project: a facts-only support and product loop for INDMoney, with HDFC Mutual Fund
scheme facts. See [docs/problemstatement.md](docs/problemstatement.md),
[docs/architecture.md](docs/architecture.md) and [docs/implementation.md](docs/implementation.md).

The full README (architecture, sources, approval gate, loops, evals, known limits) is written in
Phase 11. This section covers local development.

## Local setup

Requirements: [uv](https://docs.astral.sh/uv/) and either Docker or a Postgres 16 database with
the pgvector extension (for example a free Neon project).

```bash
cp .env.example .env          # then fill in GROQ_API_KEY and, if not using Docker, DATABASE_URL
cd backend
uv sync                       # installs Python 3.12 dependencies
```

**With Docker** (from the repo root):

```bash
docker compose up --build     # Postgres with pgvector + API on http://localhost:8000
```

**Without Docker**, point `DATABASE_URL` at your database, then from `backend/`:

```bash
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

Check it: `curl http://localhost:8000/health`

## Tests

From `backend/`:

```bash
uv run pytest -m "not llm"           # everything except real Groq calls (CI runs this)
uv run pytest -m llm                 # real Groq calls, uses free-tier quota
uv run ruff check . && uv run mypy   # lint and type checks
```

Database tests skip when no database is reachable. Set `REQUIRE_DB=1` to make that a failure.

## Corpus

From `backend/`:

```bash
uv run python -m corpus.ingest           # fetch, parse, chunk, embed and store every ready source
uv run python -m corpus.ingest --list    # the source list as a Markdown table
uv run python ../evals/run_retrieval.py  # retrieval test set (top-4 hit rate)
uv run python ../evals/run_facts.py      # Facts Agent test set (uses Groq, cached)
```

HDFC documents must be downloaded by hand first. See [backend/corpus/manual/README.md](backend/corpus/manual/README.md).
