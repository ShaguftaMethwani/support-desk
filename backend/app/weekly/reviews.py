"""Review ingestion (architecture section 5.2): load the CSV, guard, window, dedupe, store.

1. Load rows with columns review_id, source, date, rating, text.
2. Every text goes through the ingress guard before it is kept, even though the committed
   sample is already redacted: a newly collected CSV must not be trusted to be clean.
3. Rows outside the run's window are dropped.
4. Exact duplicates (same text after normalising case and spacing) and near-duplicates
   (embedding cosine similarity at or above the threshold) are dropped. The newest review of
   a duplicate group is kept.
5. The rest are upserted into `review` with their embeddings, and their ids become the run's
   review set. Later stages use exactly these ids, never "every review in the window", so
   another CSV or an older run can't leak in. Nothing is deleted: older runs' themes keep
   pointing at the reviews they were built from.
"""

import csv
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import numpy as np
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.corpus.embed import Embedder
from app.db.models import Review
from app.guard import guard

_SPACE = re.compile(r"\s+")
UPSERT_BATCH = 200


class ReviewCsvError(ValueError):
    pass


@dataclass(frozen=True)
class ReviewRow:
    review_id: str
    source: str
    date: date
    rating: int | None
    text: str  # redacted
    pii_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class IngestStats:
    rows: int
    in_window: int
    exact_duplicates: int
    near_duplicates: int
    stored: int
    reviews_with_pii: int
    review_ids: tuple[str, ...] = ()  # the run's reviews: in window, deduplicated

    def as_dict(self) -> dict[str, object]:
        return {**self.__dict__, "review_ids": list(self.review_ids)}


def load_csv(path: Path) -> list[ReviewRow]:
    """Rows from the reviews CSV, with every text passed through the ingress guard."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = {"review_id", "source", "date", "rating", "text"} - set(reader.fieldnames or [])
        if missing:
            raise ReviewCsvError(f"{path.name} is missing columns: {', '.join(sorted(missing))}")
        rows = []
        for line, raw in enumerate(reader, start=2):
            text = _SPACE.sub(" ", raw["text"] or "").strip()
            if not raw["review_id"] or not text:
                continue
            try:
                when = date.fromisoformat(raw["date"].strip()[:10])
            except ValueError as exc:
                raise ReviewCsvError(f"{path.name} line {line}: bad date") from exc
            rating = int(raw["rating"]) if (raw["rating"] or "").strip().isdigit() else None
            clean = guard(text)
            rows.append(ReviewRow(raw["review_id"].strip(), raw["source"].strip(), when, rating,
                                  clean.text, tuple(clean.pii_flags)))  # fmt: skip
    return rows


def _norm(text: str) -> str:
    return _SPACE.sub(" ", text.lower()).strip()


def dedupe(
    rows: list[ReviewRow], embeddings: list[list[float]], threshold: float
) -> tuple[list[int], int, int]:
    """Indices of rows to keep, plus counts of exact and near duplicates dropped.

    Rows are considered newest first, so the newest of a duplicate group is kept.
    """
    order = sorted(range(len(rows)), key=lambda i: (rows[i].date, rows[i].review_id), reverse=True)
    vectors = np.asarray(embeddings, dtype=np.float64)
    if len(vectors):
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.where(norms == 0, 1.0, norms)
    seen_text: set[str] = set()
    kept: list[int] = []
    exact = near = 0
    for i in order:
        key = _norm(rows[i].text)
        if key in seen_text:
            exact += 1
            continue
        if kept and float((vectors[kept] @ vectors[i]).max()) >= threshold:
            near += 1
            continue
        seen_text.add(key)
        kept.append(i)
    return sorted(kept), exact, near


async def ingest_reviews(
    session: AsyncSession,
    rows: list[ReviewRow],
    embedder: Embedder,
    *,
    window_start: date,
    window_end: date,
    dedupe_similarity: float,
) -> IngestStats:
    in_window = [r for r in rows if window_start <= r.date <= window_end]
    # Reuse stored embeddings for reviews already ingested with the same text.
    stored = {
        r.review_id: r
        for r in (
            await session.scalars(
                select(Review).where(Review.review_id.in_([r.review_id for r in in_window]))
            )
        ).all()
    }
    embeddings: list[list[float] | None] = [
        list(stored[r.review_id].embedding)  # type: ignore[arg-type]
        if r.review_id in stored
        and stored[r.review_id].text_redacted == r.text
        and stored[r.review_id].embedding is not None
        else None
        for r in in_window
    ]
    todo = [i for i, e in enumerate(embeddings) if e is None]
    for i, vector in zip(todo, embedder.embed_documents([in_window[i].text for i in todo]),
                         strict=True):  # fmt: skip
        embeddings[i] = vector
    vectors: list[list[float]] = [e for e in embeddings if e is not None]
    keep, exact, near = dedupe(in_window, vectors, dedupe_similarity)
    values = [
        {
            "review_id": in_window[i].review_id,
            "source": in_window[i].source,
            "date": in_window[i].date,
            "rating": in_window[i].rating,
            "text_redacted": in_window[i].text,
            "pii_flags": list(in_window[i].pii_flags),
            "embedding": vectors[i],
        }
        for i in keep
    ]
    # Batched upserts: one round trip per batch, not per review (the database may be remote).
    for batch in range(0, len(values), UPSERT_BATCH):
        stmt = insert(Review).values(values[batch : batch + UPSERT_BATCH])
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[Review.review_id],
                set_={k: stmt.excluded[k] for k in values[0] if k != "review_id"},
            )
        )
    return IngestStats(
        rows=len(rows),
        in_window=len(in_window),
        exact_duplicates=exact,
        near_duplicates=near,
        stored=len(keep),
        reviews_with_pii=sum(1 for r in in_window if r.pii_flags),
        review_ids=tuple(in_window[i].review_id for i in keep),
    )
