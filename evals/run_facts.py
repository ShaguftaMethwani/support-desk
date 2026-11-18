"""Facts Agent evaluation (Phase 2 exit check).

Run from backend/:  uv run python ../evals/run_facts.py
- Answerable questions: the agent must answer, and cite a chunk from an expected source.
  Questions marked `expect_not_covered` are ones whose answer the corpus only shows as an
  image (e.g. a riskometer dial): there the agent must say "not covered" instead of guessing.
- Out-of-scope questions: the agent must say "not covered" and report what it searched.
Uses the real Groq API (cached in Postgres, so re-runs are free).
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.agents.facts import FactsAgent  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.corpus.embed import FastEmbedder  # noqa: E402
from app.corpus.retrieve import Retriever  # noqa: E402
from app.db.models import CorpusSource  # noqa: E402
from app.db.session import get_sessionmaker  # noqa: E402
from app.llm import LLMClient  # noqa: E402
from app.llm.cache import PostgresCache  # noqa: E402

DATASETS = ROOT / "evals" / "datasets"


def load(name: str) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(line) for line in (DATASETS / name).read_text().splitlines() if line]


async def main() -> int:
    settings = get_settings()
    sessions = get_sessionmaker()
    llm = LLMClient(settings, PostgresCache(sessions))
    async with sessions() as session:
        ingested = set((await session.scalars(select(CorpusSource.key))).all())
        agent = FactsAgent(
            Retriever(session, FastEmbedder()), llm, min_similarity=settings.retrieval_min_similarity
        )
        ok_answers = scored = skipped = 0
        for q in load("retrieval.jsonl"):
            if not ingested & set(q["expect_sources"]):
                skipped += 1
                continue
            scored += 1
            r = await agent.run(q["question"], scheme=q["scheme"])
            cited = r.citation.source_key if r.citation else None
            if q.get("expect_not_covered"):
                good = r.status == "not_covered" and bool(r.searched)
            else:
                good = r.status == "answered" and cited in q["expect_sources"]
            ok_answers += good
            print(f"{'PASS' if good else 'FAIL'} {q['id']} {r.status:11} {cited or r.reason}")
            print(f"     Q: {q['question']}")
            print(f"     A: {r.answer or r.searched_summary}")
        ok_oos = 0
        oos = load("out_of_scope.jsonl")
        for q in oos:
            r = await agent.run(q["question"])
            good = r.status == "not_covered" and bool(r.searched)
            ok_oos += good
            print(f"{'PASS' if good else 'FAIL'} {q['id']} {r.status:11} {r.reason}  | {r.searched_summary}")
    print(f"\nanswered with a correct citation: {ok_answers}/{scored} (skipped {skipped} pending)")
    print(f"out-of-scope returned not covered: {ok_oos}/{len(oos)}")
    return 0 if ok_answers == scored and ok_oos == len(oos) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
