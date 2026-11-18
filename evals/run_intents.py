"""Intent classifier on the labelled set (Phase 4).

Run from backend/:  uv run python ../evals/run_intents.py
Uses the real Groq small model (cached in Postgres, so re-runs are free). Pre-rule rows never
reach the model.
"""

import asyncio
import collections
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.config import get_settings  # noqa: E402
from app.db.session import get_sessionmaker  # noqa: E402
from app.intent.classifier import classify  # noqa: E402
from app.intent.splitter import share_slots  # noqa: E402
from app.llm import LLMClient  # noqa: E402
from app.llm.cache import PostgresCache  # noqa: E402

DATASET = ROOT / "evals" / "datasets" / "intents_labelled.jsonl"


async def main() -> int:
    settings = get_settings()
    llm = LLMClient(settings, PostgresCache(get_sessionmaker()))
    rows = [json.loads(line) for line in DATASET.read_text().splitlines() if line]
    correct = 0
    confusion: collections.Counter[tuple[str, str]] = collections.Counter()
    for row in rows:
        [part] = share_slots([row["text"]])
        result = await classify(llm, part, session_scheme=None, advice_refused=False,
                                min_confidence=settings.intent_min_confidence,
                                sticky_min_confidence=settings.sticky_advice_min_confidence)  # fmt: skip
        ok = result.intent == row["intent"]
        correct += ok
        confusion[(row["intent"], result.intent)] += 1
        mark = "PASS" if ok else "FAIL"
        print(f"{mark} {row['id']} want {row['intent']:13} got {result.intent:13} "
              f"{result.method:14} {result.confidence:.2f}  {row['text'][:60]}")  # fmt: skip
    print(f"\naccuracy: {correct}/{len(rows)} = {correct / len(rows):.0%}")
    refusals = [r for r in rows if r["intent"] in ("advice", "returns")]
    print(f"refusal intents caught by pre-rules: "
          f"{sum(1 for r in refusals if r['pre_rule'])}/{len(refusals)}")  # fmt: skip
    wrong = {k: v for k, v in confusion.items() if k[0] != k[1]}
    if wrong:
        print("confusions (want -> got):", {f"{a}->{b}": n for (a, b), n in wrong.items()})
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
