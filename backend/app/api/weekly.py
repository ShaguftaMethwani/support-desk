"""Weekly loop API (architecture section 13).

POST /weekly/run      start a run in the background and return its id at once
GET  /weekly/{run_id} the run's status, stages, themes, quotes, pulse, explainer and actions

GET  /weekly/runs     recent runs, newest first

The cron job sends the trigger token, as a Bearer token or X-Trigger-Token. A signed-in user
(session cookie) may call them too, from the weekly pulse view. The local developer user that
stands in for a login on other endpoints is not enough here.
"""

import hmac
import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import session_user
from app.config import Settings, get_settings
from app.db.models import AppUser, FeeExplainer, PendingAction, Pulse, Theme, WeeklyRun
from app.db.session import get_session
from app.weekly.pipeline import STAGES, create_run, support_themes
from app.weekly.runner import WeeklyRunner, get_weekly_runner

router = APIRouter(prefix="/weekly", tags=["weekly"])


def require_trigger_token(
    settings: Annotated[Settings, Depends(get_settings)],
    user: Annotated[AppUser | None, Depends(session_user)],
    authorization: Annotated[str | None, Header()] = None,
    x_trigger_token: Annotated[str | None, Header()] = None,
) -> None:
    if user is not None:
        return
    expected = settings.weekly_trigger_token
    if expected is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "weekly trigger not configured")
    given = x_trigger_token or ""
    if authorization and authorization.lower().startswith("bearer "):
        given = authorization[7:].strip()
    if not hmac.compare_digest(given.encode(), expected.get_secret_value().encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid trigger token")


class TriggerRequest(BaseModel):
    triggered_by: Literal["cron", "manual", "eval"] = "manual"
    window_end: date | None = None  # default: today


Session = Annotated[AsyncSession, Depends(get_session)]
Runner = Annotated[WeeklyRunner, Depends(get_weekly_runner)]


@router.post(
    "/run", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_trigger_token)]
)
async def trigger_run(
    session: Session,
    runner: Runner,
    background: BackgroundTasks,
    settings: Annotated[Settings, Depends(get_settings)],
    body: TriggerRequest | None = None,
) -> Any:
    body = body or TriggerRequest()
    run, created = await create_run(
        session,
        window_end=body.window_end or date.today(),
        weeks=settings.weekly_window_weeks,
        triggered_by=body.triggered_by,
    )
    await session.commit()
    if not created:
        return JSONResponse(
            {"run_id": str(run.id), "status": run.status, "detail": "a run is already active"},
            status_code=status.HTTP_409_CONFLICT,
        )
    background.add_task(runner.run, run.id)
    return {"run_id": str(run.id), "status": run.status}


@router.get("/runs", dependencies=[Depends(require_trigger_token)])
async def list_runs(
    session: Session, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> dict[str, Any]:
    runs = (
        await session.scalars(select(WeeklyRun).order_by(WeeklyRun.started_at.desc()).limit(limit))
    ).all()
    return {
        "runs": [
            {
                "run_id": str(r.id),
                "status": r.status,
                "error": r.error,
                "triggered_by": r.triggered_by,
                "window": {"start": r.window_start.isoformat(), "end": r.window_end.isoformat()},
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            }
            for r in runs
        ]
    }


@router.get("/{run_id}", dependencies=[Depends(require_trigger_token)])
async def get_run(run_id: uuid.UUID, session: Session) -> dict[str, Any]:
    run = await session.get(WeeklyRun, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "run not found")
    themes = (await session.scalars(select(Theme).where(Theme.run_id == run.id))).all()
    pulse = await session.scalar(select(Pulse).where(Pulse.run_id == run.id))
    explainer = await session.scalar(select(FeeExplainer).where(FeeExplainer.run_id == run.id))
    actions = (
        await session.scalars(
            select(PendingAction)
            .where(PendingAction.origin == "weekly_run", PendingAction.origin_id == run.id)
            .order_by(PendingAction.created_at)
        )
    ).all()
    log = run.stage_log or {}
    window = {"start": run.window_start.isoformat(), "end": run.window_end.isoformat()}
    support_windows = {t["theme_id"]: t["window"] for t in support_themes(run)}
    return {
        "run_id": str(run.id),
        "status": run.status,
        "error": run.error,
        "triggered_by": run.triggered_by,
        "window": window,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "stages": {name: (log.get(name) or {}).get("status", "pending") for name in STAGES},
        "stage_log": log,
        "themes": [
            {
                "id": str(t.id),
                "rank": t.rank,
                "label": t.label,
                "description": t.description,
                "count": t.count,
                "review_ids": t.review_ids,
                "question_ids": t.question_ids,
                "from_support_queue": t.from_support_queue,
                "window": support_windows.get(str(t.id), window),
            }
            for t in sorted(themes, key=lambda t: (t.from_support_queue, t.rank or 99, -t.count))
        ],  # fmt: skip
        "quotes": (log.get("quotes") or {}).get("quotes", []),
        "fees": log.get("fees"),
        "pulse": {"text": pulse.text_redacted, "word_count": pulse.word_count} if pulse else None,
        "explainer": (
            {
                "fee_type": explainer.fee_type,
                "bullets": explainer.bullets,
                "source_urls": explainer.source_urls,
                "last_checked": explainer.last_checked.isoformat(),
                "evidence_review_ids": explainer.evidence_review_ids,
            }
            if explainer
            else None
        ),  # fmt: skip
        "actions": [
            {
                "id": str(a.id),
                "kind": a.kind,
                "status": a.status,
                "target": a.target,
                "payload_hash": a.payload_hash,
            }
            for a in actions
        ],  # fmt: skip
    }
