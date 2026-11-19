"""The eval harness: a seeded eval database, the real API running against it, and helpers.

The API runs as a subprocess (uvicorn) with:
- DATABASE_URL pointing at the eval database (EVAL_DATABASE_URL, default: local Postgres),
- the fake MCP server, so approved actions are recorded in a file instead of reaching Google,
- JSON logs written to a file, which the grounding suite searches for PII,
- the eval reviews CSV (the review sample plus the adversarial reviews).
Evals talk to it over HTTP like any client, and read the database directly only to check it.
"""

import asyncio
import csv
import json
import os
import re
import secrets
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import asyncpg
import httpx

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"
BACKEND = ROOT / "backend"
DATASETS = EVALS / "datasets"
WORK = EVALS / ".work"  # logs and generated files; not committed
DEFAULT_DB = "postgresql+asyncpg://support:support@localhost:5432/support_desk_eval"
PORT = int(os.environ.get("EVAL_PORT", "8100"))


def load_jsonl(name: str) -> list[dict[str, Any]]:
    path = DATASETS / name
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def plain_url(url: str) -> str:
    return url.replace("postgresql+asyncpg://", "postgresql://")


# --- database ---------------------------------------------------------------------------------


@dataclass
class Db:
    url: str

    async def _fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        conn = await asyncpg.connect(plain_url(self.url))
        try:
            return [dict(r) for r in await conn.fetch(sql, *args)]
        finally:
            await conn.close()

    def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return asyncio.run(self._fetch(sql, *args))

    def value(self, sql: str, *args: Any) -> Any:
        rows = self.fetch(sql, *args)
        return next(iter(rows[0].values())) if rows else None

    def tables(self) -> list[str]:
        return [r["tablename"] for r in self.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        )]  # fmt: skip

    def text_columns(self) -> list[tuple[str, str, str]]:
        """(table, column, type) for every text and JSON column."""
        rows = self.fetch(
            "SELECT table_name, column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND data_type IN ('text','character varying','jsonb','json')"
            " ORDER BY table_name, column_name"
        )
        return [(r["table_name"], r["column_name"], r["data_type"]) for r in rows]


# Columns left out of snapshots: embeddings (large, derived) and the tsvector.
_SKIP_SNAPSHOT_TYPES = {"USER-DEFINED", "tsvector"}


def snapshot(
    db: Db, *, exclude: frozenset[str] = frozenset({"llm_cache"})
) -> dict[str, dict[str, str]]:
    """Every table as {primary key: row as sorted JSON}. Embeddings and the tsvector are left
    out; the Groq cache is left out by default (a cache write is not a state change)."""

    async def run() -> dict[str, dict[str, str]]:
        conn = await asyncpg.connect(plain_url(db.url))
        try:
            out: dict[str, dict[str, str]] = {}
            tables = [r["tablename"] for r in await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'")]  # fmt: skip
            for table in sorted(set(tables) - exclude):
                cols = await conn.fetch(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=$1 ORDER BY ordinal_position",
                    table,
                )
                keep = [
                    c["column_name"] for c in cols if c["data_type"] not in _SKIP_SNAPSHOT_TYPES
                ]
                pk = await conn.fetch(
                    "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid"
                    " AND a.attnum = ANY(i.indkey) WHERE i.indrelid = $1::regclass AND i.indisprimary",
                    table,
                )
                key_cols = [r["attname"] for r in pk] or keep
                select = ", ".join(f'"{c}"' for c in keep)
                rows = await conn.fetch(f'SELECT {select} FROM "{table}"')
                out[table] = {
                    "|".join(str(r[k]) for k in key_cols): json.dumps(
                        {k: r[k] for k in keep}, sort_keys=True, default=str
                    )
                    for r in rows
                }
            return out
        finally:
            await conn.close()

    return asyncio.run(run())


@dataclass
class Diff:
    added: dict[str, set[str]] = field(default_factory=dict)
    removed: dict[str, set[str]] = field(default_factory=dict)
    changed: dict[str, set[str]] = field(default_factory=dict)

    def tables(self) -> set[str]:
        return set(self.added) | set(self.removed) | set(self.changed)

    def summary(self) -> dict[str, dict[str, int]]:
        return {
            t: {"added": len(self.added.get(t, ())), "removed": len(self.removed.get(t, ())),
                "changed": len(self.changed.get(t, ()))}
            for t in sorted(self.tables())
        }  # fmt: skip


def diff(before: dict[str, dict[str, str]], after: dict[str, dict[str, str]]) -> Diff:
    d = Diff()
    for table in set(before) | set(after):
        b, a = before.get(table, {}), after.get(table, {})
        if added := set(a) - set(b):
            d.added[table] = added
        if removed := set(b) - set(a):
            d.removed[table] = removed
        if changed := {k for k in set(a) & set(b) if a[k] != b[k]}:
            d.changed[table] = changed
    return d


# --- the API under test -----------------------------------------------------------------------


@dataclass
class EvalEnv:
    db: Db
    base_url: str
    trigger_token: str
    log_path: Path
    mcp_log: Path
    reviews_csv: Path
    started_at: datetime
    client: httpx.Client

    # chat -----------------------------------------------------------------------------------
    def chat(
        self, text: str, *, channel: str = "text", session_id: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text, "channel": channel}
        if session_id:
            body["session_id"] = session_id
        r = self.client.post("/chat/message", json=body)
        r.raise_for_status()
        return r.json()

    def voice(self, clip: Path) -> dict[str, Any]:
        kind = {"wav": "audio/wav", "m4a": "audio/mp4"}[clip.suffix[1:]]
        r = self.client.post("/chat/voice", content=clip.read_bytes(),
                             headers={"content-type": kind})  # fmt: skip
        r.raise_for_status()
        return r.json()

    def item(self, item_id: str) -> dict[str, Any]:
        r = self.client.get(f"/queue/{item_id}")
        r.raise_for_status()
        return r.json()

    # weekly ---------------------------------------------------------------------------------
    def weekly_headers(self) -> dict[str, str]:
        return {"X-Trigger-Token": self.trigger_token}

    def run_weekly(self, window_end: str, timeout_s: float = 1800) -> dict[str, Any]:
        r = self.client.post("/weekly/run", headers=self.weekly_headers(),
                             json={"triggered_by": "eval", "window_end": window_end})  # fmt: skip
        if r.status_code not in (202, 409):
            r.raise_for_status()
        run_id = r.json()["run_id"]
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            run = self.weekly(run_id)
            if run["status"] not in ("pending", "in_progress"):
                return run
            time.sleep(3)
        raise TimeoutError(f"weekly run {run_id} did not finish in {timeout_s} s")

    def weekly(self, run_id: str) -> dict[str, Any]:
        r = self.client.get(f"/weekly/{run_id}", headers=self.weekly_headers())
        r.raise_for_status()
        return r.json()

    # gate -----------------------------------------------------------------------------------
    def pending_actions(self) -> list[dict[str, Any]]:
        r = self.client.get("/actions/pending")
        r.raise_for_status()
        return r.json()["actions"]

    def approve_action(self, action: dict[str, Any]) -> httpx.Response:
        return self.client.post(f"/actions/{action['id']}/approve",
                                json={"payload_hash": action["payload_hash"]})  # fmt: skip

    # logs -----------------------------------------------------------------------------------
    def log_events(self) -> list[dict[str, Any]]:
        events = []
        for line in self.log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return events

    def mcp_calls(self) -> list[dict[str, Any]]:
        if not self.mcp_log.exists():
            return []
        return [json.loads(line) for line in self.mcp_log.read_text().splitlines() if line]


def build_reviews_csv(out: Path) -> Path:
    """The review sample plus the adversarial reviews, which the weekly loop must handle."""
    sample = ROOT / "data" / "reviews_sample.csv"
    with sample.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    latest = max(r["date"] for r in rows)
    for r in load_jsonl("adversarial_reviews.jsonl"):
        rows.append({"review_id": r["review_id"], "source": "play_store",
                     "date": r.get("date", latest), "rating": str(r["rating"]), "text": r["text"]})  # fmt: skip
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["review_id", "source", "date", "rating", "text"])
        writer.writeheader()
        writer.writerows(rows)
    return out


def latest_review_date() -> str:
    with (ROOT / "data" / "reviews_sample.csv").open(newline="", encoding="utf-8") as f:
        return max(r["date"] for r in csv.DictReader(f))


def seed(
    db_url: str, reviews_csv: Path, *, reseed: bool, fresh: bool, import_cache_from: str | None
) -> None:
    args = [sys.executable, str(EVALS / "seed.py"), "--reviews-csv", str(reviews_csv)]
    if reseed:
        args.append("--reseed")
    if fresh:
        args.append("--fresh")
    env = {**os.environ, "DATABASE_URL": db_url}
    if import_cache_from:
        # In the environment, not argv: the URL holds a password, and a failed command's
        # arguments end up in error messages and reports.
        args.append("--import-cache")
        env["EVAL_IMPORT_CACHE_URL"] = import_cache_from
    result = subprocess.run(args, cwd=BACKEND, env=env)
    if result.returncode:
        raise RuntimeError(
            f"seeding the eval database failed (exit {result.returncode}); see above"
        )


def needs_reseed(db: Db) -> bool:
    try:
        return not db.value("SELECT count(*) FROM corpus_chunk")
    except Exception:  # noqa: BLE001 - no schema yet
        return True


@contextmanager
def running_api(db_url: str, reviews_csv: Path, stamp: str) -> Iterator[EvalEnv]:
    WORK.mkdir(exist_ok=True)
    log_path = WORK / f"server-{stamp}.log"
    mcp_log = WORK / f"mcp-{stamp}.jsonl"
    token = secrets.token_urlsafe(16)
    env = {
        **os.environ,
        "APP_ENV": "local",  # requests without a login act as the local user
        "DATABASE_URL": db_url,
        "LOG_JSON": "true",
        "LOG_LEVEL": "INFO",
        "WEEKLY_TRIGGER_TOKEN": token,
        "WEEKLY_RESUME_ON_STARTUP": "false",
        "REVIEWS_CSV_PATH": str(reviews_csv),
        "GOOGLE_DOC_ID": "eval-doc",
        # Never real Google credentials: the fake server ignores them anyway.
        "GOOGLE_CLIENT_ID": "eval",
        "GOOGLE_CLIENT_SECRET": "eval",
        "GOOGLE_REFRESH_TOKEN": "eval",
        "MCP_SERVER_COMMAND": f"{sys.executable} {EVALS / 'fake_mcp_server.py'} {mcp_log}",
    }
    started = datetime.now()
    with log_path.open("w") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT),
             "--host", "127.0.0.1"],
            cwd=BACKEND, env=env, stdout=log, stderr=subprocess.STDOUT,
        )  # fmt: skip
        base = f"http://127.0.0.1:{PORT}"
        try:
            for _ in range(120):
                if proc.poll() is not None:
                    raise RuntimeError(f"API exited early; see {log_path}")
                try:
                    if httpx.get(f"{base}/health", timeout=2).status_code in (200, 503):
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.5)
            with httpx.Client(base_url=base, timeout=300) as client:
                yield EvalEnv(
                    Db(db_url), base, token, log_path, mcp_log, reviews_csv, started, client
                )
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


_TOKEN = re.compile(r"\[[A-Z_]+_REDACTED\]")
_UUID = re.compile(r"\b[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}\b", re.I)


def without_tokens(text: str) -> str:
    """Text with redaction tokens and ids (UUIDs, which hold digit runs) removed, so the PII
    detectors only see what could be personal data."""
    return _UUID.sub(" ", _TOKEN.sub(" ", text))
