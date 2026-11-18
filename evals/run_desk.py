"""Phase 4 exit check: 20 real complaints from the reviews through the response loop.

Run from backend/:  uv run python ../evals/run_desk.py
Checks, for every complaint:
- each agent the route needs returned structured output (all three unless it was refused)
- the item ended with a draft: an answer with a citation, a refusal with its link, a
  clarifying question, or "not covered"
- the agents' run times overlap (they ran in parallel)
and that a blocked draft can't be approved. Uses the real Groq models (cached in Postgres).
Items are created as CSV imports and stay in the queue.
"""

import asyncio
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from sqlalchemy import select  # noqa: E402

from app.db.models import AgentRun, Review  # noqa: E402
from app.orchestrator.deps import get_desk_deps  # noqa: E402
from app.orchestrator.desk import (  # noqa: E402
    DeskError,
    approve_item,
    intake_message,
    latest_draft,
    process_message,
)

N = 20
_TOKEN = re.compile(r"\[[A-Z_]+_REDACTED\]")
_WORD = re.compile(r"[A-Za-z]+")


def is_complaint_sample(text: str) -> bool:
    """Long enough, mostly English, no redactions (so the output is readable)."""
    words = _WORD.findall(text)
    ascii_share = sum(c.isascii() for c in text) / max(len(text), 1)
    return len(words) >= 12 and ascii_share > 0.95 and not _TOKEN.search(text)


async def main() -> int:
    deps = get_desk_deps()
    async with deps.sessions() as s:
        rows = (await s.scalars(
            select(Review).where(Review.rating <= 2).order_by(Review.date.desc(), Review.review_id)
        )).all()  # fmt: skip
        picked = [r for r in rows if is_complaint_sample(r.text_redacted)][:N]
        item_ids = []
        for review in picked:
            intake = await intake_message(s, deps, review.text_redacted, channel="csv",
                                          session_id=None)  # fmt: skip
            item_ids.extend(intake.item_ids)
        await s.commit()
    print(f"{len(picked)} complaints -> {len(item_ids)} queue items")

    started = time.monotonic()
    outcomes = await process_message(deps, item_ids, None)
    print(f"processed in {time.monotonic() - started:.0f}s\n")

    ok_agents = ok_draft = overlapping = 0
    blocked = []
    async with deps.sessions() as s:
        for outcome in outcomes:
            runs = (await s.scalars(select(AgentRun).where(AgentRun.item_id == outcome.item_id))).all()
            draft = await latest_draft(s, outcome.item_id)
            expected = {"pattern", "compliance"} | (
                set() if outcome.intent in ("advice", "returns", "unclear") else {"facts"}
            )
            agents_ok = {r.agent for r in runs if r.status == "ok"} == expected
            ok_agents += agents_ok
            has_draft = draft is not None and (
                draft.citation is not None or draft.source_type in ("not_covered", "clarification")
            )
            ok_draft += has_draft
            overlap = bool(runs) and max(r.started_at for r in runs) <= min(
                r.finished_at for r in runs if r.finished_at
            )
            overlapping += overlap
            if draft is not None and draft.review_status == "blocked":
                blocked.append(outcome.item_id)
            team = (draft.routing or {}).get("team") if draft else None
            print(f"{'PASS' if agents_ok and has_draft else 'FAIL'} {outcome.intent or '-':13} "
                  f"{sorted(r.agent for r in runs)} {draft.source_type if draft else 'no draft':12} "
                  f"review={draft.review_status if draft else '-':8} team={team} "
                  f"overlap={overlap}")  # fmt: skip

    refused = 0
    for item_id in blocked:
        try:
            await approve_item(deps, item_id, user_id=None)  # type: ignore[arg-type]
        except DeskError:
            refused += 1
    print(f"\nall needed agents returned output: {ok_agents}/{len(outcomes)}")
    print(f"ended with a cited draft or not covered: {ok_draft}/{len(outcomes)}")
    print(f"agent runs overlapped in time: {overlapping}/{len(outcomes)}")
    print(f"blocked drafts: {len(blocked)}, approval refused for {refused}/{len(blocked)}")
    if hasattr(deps.llm, "_cache") and deps.llm._cache is not None:  # noqa: SLF001
        await deps.llm._cache.flush()  # type: ignore[union-attr]  # noqa: SLF001
    return 0 if ok_agents == ok_draft == overlapping == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
