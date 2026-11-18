"""Pattern Agent (architecture section 7.4). No model call.

Checks a question against the reviews and recent queue questions:
- similar reviews: cosine similarity to the question at or above `similarity`, dated within the
  window (pgvector, counted in SQL)
- similar questions: other queue items in the same rolling question cluster, in the window
- the nearest theme of the latest weekly run, when its centroid is close enough

Labels:
- rising: this week's similar items are at least double the previous week's, and at least
  `rising_min`
- recurring: at least `recurring_min` similar items in the window
- one_off: otherwise
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.connector.clusters import vector_literal

TOP_EVIDENCE = 20


class PatternResult(BaseModel):
    label: Literal["recurring", "rising", "one_off"]
    matched_theme_id: str | None = None
    matched_theme_label: str | None = None
    theme_similarity: float | None = None
    review_count: int
    question_count: int
    this_week: int
    previous_week: int
    window_start: date
    window_end: date
    evidence_ids: list[str] = Field(default_factory=list)  # review ids and queue item ids


@dataclass(frozen=True)
class PatternSettings:
    similarity: float
    theme_similarity: float
    window_days: int
    recurring_min: int
    rising_min: int


_REVIEWS_SQL = text("""
    SELECT count(*) AS total,
           count(*) FILTER (WHERE date > :week1) AS this_week,
           count(*) FILTER (WHERE date > :week2 AND date <= :week1) AS previous_week,
           (array_agg(review_id ORDER BY embedding <=> CAST(:v AS vector)))[1:20] AS ids
    FROM review
    WHERE embedding IS NOT NULL
      AND date BETWEEN :start AND :end
      AND embedding <=> CAST(:v AS vector) <= :max_distance
""")

_QUESTIONS_SQL = text("""
    SELECT count(*) AS total,
           count(*) FILTER (WHERE created_at::date > :week1) AS this_week,
           count(*) FILTER (WHERE created_at::date > :week2
                            AND created_at::date <= :week1) AS previous_week,
           (array_agg(id::text ORDER BY created_at DESC))[1:20] AS ids
    FROM queue_item
    WHERE cluster_id = :cluster AND id <> :item
      AND created_at::date BETWEEN :start AND :end
""")

_THEME_SQL = text("""
    SELECT t.id::text AS id, t.label, 1 - (t.centroid <=> CAST(:v AS vector)) AS sim
    FROM theme t
    WHERE t.centroid IS NOT NULL AND t.run_id = (
        SELECT r.id FROM weekly_run r
        WHERE r.status IN ('awaiting_approval', 'completed')
        ORDER BY r.started_at DESC LIMIT 1)
    ORDER BY t.centroid <=> CAST(:v AS vector)
    LIMIT 1
""")


def label_for(total: int, this_week: int, previous_week: int, cfg: PatternSettings) -> str:
    if this_week >= cfg.rising_min and this_week >= 2 * previous_week:
        return "rising"
    if total >= cfg.recurring_min:
        return "recurring"
    return "one_off"


async def run_pattern(
    s: AsyncSession,
    embedding: list[float],
    *,
    item_id: uuid.UUID,
    cluster_id: uuid.UUID | None,
    now: datetime,
    cfg: PatternSettings,
) -> PatternResult:
    end = now.date()
    start = end - timedelta(days=cfg.window_days - 1)
    week1, week2 = end - timedelta(days=7), end - timedelta(days=14)
    v = vector_literal(embedding)
    window = {"start": start, "end": end, "week1": week1, "week2": week2}
    reviews = (
        await s.execute(_REVIEWS_SQL, {**window, "v": v, "max_distance": 1 - cfg.similarity})
    ).one()
    questions = None
    if cluster_id is not None:
        questions = (
            await s.execute(_QUESTIONS_SQL, {**window, "cluster": cluster_id, "item": item_id})
        ).one()
    theme = (await s.execute(_THEME_SQL, {"v": v})).first()

    q_total = int(questions.total) if questions else 0
    total = int(reviews.total) + q_total
    this_week = int(reviews.this_week) + (int(questions.this_week) if questions else 0)
    previous = int(reviews.previous_week) + (int(questions.previous_week) if questions else 0)
    matched = theme is not None and float(theme.sim) >= cfg.theme_similarity
    return PatternResult(
        label=label_for(total, this_week, previous, cfg),
        matched_theme_id=theme.id if matched and theme else None,
        matched_theme_label=theme.label if matched and theme else None,
        theme_similarity=round(float(theme.sim), 3) if theme is not None else None,
        review_count=int(reviews.total),
        question_count=q_total,
        this_week=this_week,
        previous_week=previous,
        window_start=start,
        window_end=end,
        evidence_ids=list(reviews.ids or [])[:TOP_EVIDENCE]
        + list(questions.ids or [] if questions else [])[:TOP_EVIDENCE],
    )
