"""Retrieval evaluation (Phase 2 exit check: the right source in the top 4 for >= 90%).

Run from backend/:  uv run python ../evals/run_retrieval.py
A question counts as a hit when one of its top-4 chunks comes from an expected source and,
if the question names a phrase, contains that phrase. Questions whose expected sources are
not ingested yet (manual HDFC downloads) are reported as skipped, not as misses.
"""

import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.corpus.embed import FastEmbedder  # noqa: E402
from app.corpus.retrieve import Retriever  # noqa: E402
from app.db.models import CorpusSource  # noqa: E402
from app.db.session import get_sessionmaker  # noqa: E402

DATASETS = ROOT / "evals" / "datasets"


def load(name: str) -> list[dict]:  # type: ignore[type-arg]
    return [json.loads(line) for line in (DATASETS / name).read_text().splitlines() if line]


async def main(k: int = 4) -> int:
    questions = load("retrieval.jsonl")
    out_of_scope = load("out_of_scope.jsonl")
    embedder = FastEmbedder()
    async with get_sessionmaker()() as session:
        ingested = set((await session.scalars(select(CorpusSource.key))).all())
        retriever = Retriever(session, embedder)
        hits = misses = skipped = 0
        in_scope_best: list[float] = []
        for q in questions:
            if not ingested & set(q["expect_sources"]):
                skipped += 1
                print(f"SKIP {q['id']}  sources not ingested yet: {', '.join(q['expect_sources'])}")
                continue
            results, _ = await retriever.search(q["question"], scheme=q["scheme"], k=k)
            in_scope_best.append(max((r.similarity for r in results), default=0.0))
            phrase = (q["expect_phrase"] or "").lower()
            ok = any(
                r.source_key in q["expect_sources"] and phrase in r.text.lower() for r in results
            )
            hits += ok
            misses += not ok
            mark = "HIT " if ok else "MISS"
            got = ", ".join(f"{r.source_key}:p{r.page}" for r in results)
            print(f"{mark} {q['id']}  {q['question'][:60]:60}  -> {got}")
        oos_best = []
        for q in out_of_scope:
            results, _ = await retriever.search(q["question"], k=k)
            oos_best.append(max((r.similarity for r in results), default=0.0))
    scored = hits + misses
    rate = hits / scored if scored else 0.0
    print(f"\nhit@{k}: {hits}/{scored} = {rate:.0%}  (skipped {skipped} pending manual sources)")
    if in_scope_best:
        print(f"best similarity, answerable questions: min {min(in_scope_best):.3f}")
    print(f"best similarity, out-of-scope questions: max {max(oos_best):.3f}")
    return 0 if rate >= 0.9 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
