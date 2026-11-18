"""Canonical explainers (architecture sections 9.2 and 13).

GET /explainers/canonical     active canonical explainers with sources and reuse count;
                              ?include_archived=true also lists the ones a newer one replaced
"""

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_user
from app.db.models import AppUser, CanonicalExplainer, FeeExplainer
from app.db.session import get_session
from app.weekly.fees import BY_KEY

router = APIRouter(prefix="/explainers", tags=["explainers"])

User = Annotated[AppUser, Depends(current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]


@router.get("/canonical")
async def canonical_explainers(
    session: Session, _user: User, include_archived: bool = False
) -> list[dict[str, Any]]:
    stmt = (
        select(CanonicalExplainer, FeeExplainer, AppUser.username)
        .join(FeeExplainer, FeeExplainer.id == CanonicalExplainer.explainer_id)
        .outerjoin(AppUser, AppUser.id == CanonicalExplainer.approved_by)
        .order_by(CanonicalExplainer.active.desc(), CanonicalExplainer.approved_at.desc())
    )
    if not include_archived:
        stmt = stmt.where(CanonicalExplainer.active)
    rows = (await session.execute(stmt)).all()
    return [
        {
            "id": str(c.id),
            "fee_type": c.fee_type,
            "label": BY_KEY[c.fee_type].label if c.fee_type in BY_KEY else c.fee_type,
            "active": c.active,
            "bullets": e.bullets,
            "source_urls": e.source_urls,
            "last_checked": e.last_checked.isoformat(),
            "explainer_id": str(e.id),
            "weekly_run_id": str(e.run_id),
            "approved_at": c.approved_at.isoformat(),
            "approved_by": username,
            "archived_at": c.archived_at.isoformat() if c.archived_at else None,
            "reuse_count": c.reuse_count,
        }
        for c, e, username in rows
    ]
