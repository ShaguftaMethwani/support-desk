"""Sources and reviews, for the frontend (architecture sections 13 and 14).

GET /sources        every corpus source with its publisher, last-checked date and the drafts
                    that cite it
GET /reviews?ids=   reviews by id (redacted text), so a theme's count can list its reviews
"""

from collections import defaultdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_user
from app.db.models import AppUser, CorpusChunk, CorpusSource, DraftVersion, QueueItem, Review
from app.db.session import get_session

router = APIRouter(tags=["sources"])

User = Annotated[AppUser, Depends(current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]

# Citing drafts listed per source. The total is always given.
CITING_LIMIT = 25


@router.get("/sources")
async def list_sources(user: User, session: Session) -> dict[str, Any]:
    order = (CorpusSource.publisher, CorpusSource.key)
    sources = (await session.scalars(select(CorpusSource).order_by(*order))).all()
    chunk_counts: dict[Any, int] = dict(
        (await session.execute(
            select(CorpusChunk.source_id, func.count()).group_by(CorpusChunk.source_id)
        )).all()
    )  # fmt: skip
    # Only the columns shown: never the embeddings or the full reply text.
    rows = await session.execute(
        select(
            DraftVersion.citation["source_key"].astext,
            DraftVersion.citation["page"].astext,
            DraftVersion.id,
            DraftVersion.version,
            DraftVersion.source_type,
            DraftVersion.review_status,
            DraftVersion.created_at,
            QueueItem.id,
            QueueItem.text_redacted,
            QueueItem.status,
        )
        .join(QueueItem, QueueItem.id == DraftVersion.item_id)
        .where(DraftVersion.citation.is_not(None))
        .order_by(DraftVersion.created_at.desc())
    )
    citing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key, page, draft_id, version, source_type, review_status, created, item_id, q, st in rows:
        citing[key].append(
            {
                "draft_id": str(draft_id),
                "item_id": str(item_id),
                "version": version,
                "question": q,
                "item_status": st,
                "source_type": source_type,
                "review_status": review_status,
                "page": int(page) if page and page.isdigit() else None,
                "created_at": created.isoformat() if created else None,
            }
        )
    return {
        "sources": [
            {
                "id": str(s.id),
                "key": s.key,
                "url": s.url,
                "publisher": s.publisher,
                "doc_type": s.doc_type,
                "title": s.title,
                "schemes": s.schemes,
                "last_checked": s.fetched_at.date().isoformat() if s.fetched_at else None,
                "chunks": chunk_counts.get(s.id, 0),
                "citing_count": len(citing.get(s.key, [])),
                "citing": citing.get(s.key, [])[:CITING_LIMIT],
            }
            for s in sources
        ]
    }


@router.get("/reviews")
async def get_reviews(
    user: User,
    session: Session,
    ids: Annotated[list[str], Query(min_length=1, max_length=200)],
) -> dict[str, Any]:
    reviews = (await session.scalars(select(Review).where(Review.review_id.in_(ids)))).all()
    found = {r.review_id: r for r in reviews}
    return {
        "reviews": [
            {
                "review_id": r.review_id,
                "source": r.source,
                "date": r.date.isoformat(),
                "rating": r.rating,
                "text": r.text_redacted,
            }
            for rid in ids
            if (r := found.get(rid)) is not None
        ],
        "missing": [rid for rid in ids if rid not in found],
    }
