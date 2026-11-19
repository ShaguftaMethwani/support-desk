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

## Frontend

The agent console: chat, queue, approvals, weekly pulse, canonical explainers and sources.
Every view is behind a login, so create a user first (from `backend/`):

```bash
uv run python -m scripts.create_user <username>   # asks for a password (10+ characters)
```

Then, with the API running on port 8000:

```bash
cd frontend
npm ci
npm run dev          # http://localhost:5173 (proxies /api to localhost:8000)
npm test             # unit tests
npm run gen:api      # regenerate src/api/schema.d.ts after changing the API
```

## Response loop

With the API running locally (`APP_ENV=local`, where curl requests without a login act as a
local developer user; the browser always asks for a login):

```bash
curl -X POST localhost:8000/chat/message -H 'content-type: application/json' \
  -d '{"text": "What is the exit load of HDFC Liquid Fund, and should I redeem now?"}'
curl localhost:8000/queue
uv run python ../evals/run_intents.py   # intent classifier on the labelled set
uv run python ../evals/run_desk.py      # exit check: 20 real complaints through the desk
```

## MCP server

[`mcp_server/`](mcp_server/) is a copy of the Google Docs and Gmail MCP server from
`ShaguftaMethwani/mcpserver-test` at commit `4677e61` (7 September 2026), changed so it can
only create drafts and append to docs: the send tool is removed, draft recipients are
optional, and appends return the revision and can be deduplicated. Details in
[mcp_server/VENDORED.md](mcp_server/VENDORED.md).

```bash
cd mcp_server && npm ci && npm test && npm run build   # 45 tests; builds dist/index.js
```

The backend starts it over stdio for each approved action; it is not hosted separately. Only
`google_docs_append` and `gmail_create_draft` can be called (`backend/app/mcp/allowlist.py`).

## Approval gate

Nothing is written to Google until a person approves that one action:

```bash
curl localhost:8000/actions/pending                     # kind, target, full payload, payload_hash
curl -X POST localhost:8000/actions/<id>/approve \
  -H 'content-type: application/json' -d '{"payload_hash": "<hash shown with the action>"}'
curl -X POST localhost:8000/actions/<id>/reject
curl -X POST localhost:8000/actions/<id>/retry          # a failed action, under its approval
```

## Voice

Spoken questions take the same path as typed ones. The browser posts the recorded clip (webm, ogg, wav, mp3, m4a or flac, at most 30 seconds) as the raw request body:

```bash
curl -X POST localhost:8000/chat/voice -H 'content-type: audio/wav' \
  --data-binary @evals/datasets/voice/clips/v05.wav
```

- **What comes back.** The transcript, already redacted, plus the PII types removed and any suggestions for misheard scheme names ("big cap" → "Mid Cap"). Suggestions are never applied automatically.
- **Confirming.** The user confirms or edits the transcript, which then goes to `POST /chat/message` with `"channel": "voice"`.
- **Spoken replies.** Each draft has a `spoken_text` field: the reply without its source lines, which stay on screen.
- **Nothing is kept.** Audio and the raw transcript are never stored or logged.
- **Groq's retention.** Transcription is done by Groq Whisper (`whisper-large-v3-turbo`). By default Groq keeps no inputs, but it may log them for up to 30 days for troubleshooting or abuse checks. Turn on Zero Data Retention in the Groq console's Data Controls to prevent even that ([Groq: your data](https://console.groq.com/docs/your-data), checked November 2026).

**Test clips.** The test clips are in `evals/datasets/voice/`. `make_clips.py` regenerates them with macOS text-to-speech. Run `uv run python ../evals/run_voice.py` from `backend/`, with the API running, to transcribe them.

## Loop connector

The two loops feed each other (architecture section 9):

- **Support questions into the pulse.** When 5 similar questions arrive within 7 days, their cluster is surfaced. The next weekly run lists it in the pulse under "From the support queue", with its count, window and queue item ids.
- **Fee explainers into replies.** Approving a weekly run's `promote_canonical` action makes its fee explainer the canonical answer. Related questions are then answered with its bullets, word for word (`source_type = canonical_explainer`).

```bash
curl localhost:8000/explainers/canonical                 # active explainers and reuse counts
```

A step-by-step demo is in [docs/demo_script.md](docs/demo_script.md).

## Weekly loop

```bash
# collect a fresh review sample (PII is removed before anything is written)
uv run --with google-play-scraper python -m scripts.collect_reviews --weeks 12
# start a run (the API must be running; the token is WEEKLY_TRIGGER_TOKEN)
curl -X POST localhost:8000/weekly/run -H "Authorization: Bearer $WEEKLY_TRIGGER_TOKEN"
curl localhost:8000/weekly/<run_id> -H "Authorization: Bearer $WEEKLY_TRIGGER_TOKEN"
```

**Exit load hypothesis:** rejected by the data. In the 12-week sample (1,085 reviews), no review
mentions exit load. The mutual fund fee issue users raise is Direct vs Regular plans (distributor
commission); most other charge complaints are about INDMoney's own brokerage and US-investing
charges, which the corpus does not cover. See [docs/implementation.md](docs/implementation.md),
Phase 3 status.
