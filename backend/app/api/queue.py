"""Chat and queue API (architecture section 13).

POST /chat/message           typed message -> one answer block per question, in order
GET  /queue                  queue items with their latest draft, flags and routing
GET  /queue/{id}             one item with every draft version, agent run and refinement
POST /queue/{id}/refine      targeted refinement (only the affected agents re-run)
PUT  /queue/{id}/draft       a human edit, as a new version, re-validated
POST /queue/{id}/approve     accept the draft: creates its pending actions, writes nothing
POST /queue/{id}/reject      reject this item only
POST /queue/import-csv       turn selected reviews into queue items
"""

import time
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_user
from app.db.models import (
    AgentRun,
    AppUser,
    DraftVersion,
    Message,
    QueueItem,
    RefinementEvent,
    Review,
    ReviewResult,
)
from app.db.session import get_session
from app.intent.session import open_session, remember, snapshot
from app.observability import get_logger
from app.orchestrator.deps import get_desk_deps
from app.orchestrator.desk import (
    DeskDeps,
    DeskError,
    approve_item,
    edit_draft,
    intake_message,
    latest_draft,
    process_message,
    refine_item,
    reject_item,
    session_ttl,
)

router = APIRouter(tags=["desk"])
log = get_logger(__name__)

User = Annotated[AppUser, Depends(current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]
Deps = Annotated[DeskDeps, Depends(get_desk_deps)]


def _http(exc: DeskError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


def spoken_text(reply: str) -> str:
    lines = [ln for ln in reply.splitlines() if not ln.startswith(("Source:", "Last checked:"))]
    return " ".join(" ".join(lines).split())


def draft_view(d: DraftVersion | None) -> dict[str, Any] | None:
    if d is None:
        return None
    return {
        "id": str(d.id),
        "version": d.version,
        "reply": d.reply_redacted,
        # What a spoken reply reads: the reply without its Source and Last checked lines,
        # which stay on screen (architecture section 11).
        "spoken_text": spoken_text(d.reply_redacted),
        "citation": d.citation,
        "last_checked": d.last_checked.isoformat() if d.last_checked else None,
        "source_type": d.source_type,
        "canonical_explainer_id": str(d.canonical_id) if d.canonical_id else None,
        "routing": d.routing,
        "author": d.author,
        "model_tag": d.model_tag,
        "review_status": d.review_status,
        "approvable": d.review_status == "passed",
    }


def item_view(item: QueueItem, draft: DraftVersion | None, channel: str | None) -> dict[str, Any]:
    return {
        "id": str(item.id),
        "message_id": str(item.message_id),
        "part_index": item.part_index,
        "question": item.text_redacted,
        "channel": channel,
        "intent": item.intent,
        "intent_confidence": item.intent_confidence,
        "slots": item.shared_slots,
        "status": item.status,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "draft": draft_view(draft),
    }


# --- chat -----------------------------------------------------------------------------------


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    session_id: uuid.UUID | None = None
    # "voice" for a transcript the user confirmed after POST /chat/voice. It is guarded again
    # here like any text: the user may have edited it.
    channel: Literal["text", "voice"] = "text"


@router.post("/chat/message")
async def chat_message(body: ChatRequest, user: User, session: Session, deps: Deps) -> Any:
    started = time.monotonic()
    chat = await open_session(session, body.session_id, user.id, ttl=session_ttl(deps.settings))
    state = snapshot(chat)
    intake = await intake_message(
        session, deps, body.text, channel=body.channel, session_id=chat.id
    )
    await session.commit()
    intake_done = time.monotonic()

    outcomes = await process_message(deps, intake.item_ids, state)
    log.info("chat_message_timing", parts=len(intake.item_ids),
             intake_ms=int((intake_done - started) * 1000),
             process_ms=int((time.monotonic() - intake_done) * 1000))  # fmt: skip
    chat = await open_session(session, chat.id, user.id, ttl=session_ttl(deps.settings))
    remember(chat, schemes=[o.scheme for o in outcomes],
             advice_refused=any(o.advice_refused for o in outcomes))  # fmt: skip
    await session.commit()

    answers = []
    for item_id in intake.item_ids:
        item = await session.get(QueueItem, item_id, populate_existing=True)
        assert item is not None
        answers.append(item_view(item, await latest_draft(session, item.id), "text"))
    dropped = None
    if intake.dropped:
        dropped = {"message": "Please ask the remaining questions separately.",
                   "questions": intake.dropped}  # fmt: skip
    return {
        "session_id": str(chat.id),
        "message_id": str(intake.message_id),
        "split_method": intake.split_method,
        "answers": answers,
        "dropped": dropped,
    }


# --- queue ----------------------------------------------------------------------------------


@router.get("/queue")
async def list_queue(
    user: User,
    session: Session,
    status: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Any:
    stmt = (
        select(QueueItem, Message.channel)
        .join(Message, Message.id == QueueItem.message_id)
        .order_by(QueueItem.created_at.desc(), QueueItem.part_index)
        .limit(limit)
    )
    if status:
        stmt = stmt.where(QueueItem.status == status)
    rows = (await session.execute(stmt)).all()
    out = []
    for item, channel in rows:
        view = item_view(item, await latest_draft(session, item.id), channel)
        flags = await session.scalar(
            select(AgentRun.output_json).where(AgentRun.item_id == item.id,
                                               AgentRun.agent == "compliance")
            .order_by(AgentRun.started_at.desc()).limit(1)
        )  # fmt: skip
        view["compliance"] = flags
        out.append(view)
    return {"items": out}


@router.get("/queue/{item_id}")
async def get_item(item_id: uuid.UUID, user: User, session: Session) -> Any:
    item = await session.get(QueueItem, item_id)
    if item is None:
        raise HTTPException(404, "queue item not found")
    message = await session.get(Message, item.message_id)
    versions = (await session.scalars(
        select(DraftVersion).where(DraftVersion.item_id == item.id).order_by(DraftVersion.version)
    )).all()  # fmt: skip
    reviews = (await session.scalars(
        select(ReviewResult).where(ReviewResult.draft_version_id.in_([v.id for v in versions]))
    )).all()  # fmt: skip
    runs = (await session.scalars(
        select(AgentRun).where(AgentRun.item_id == item.id).order_by(AgentRun.started_at)
    )).all()  # fmt: skip
    events = (await session.scalars(
        select(RefinementEvent).where(RefinementEvent.item_id == item.id)
        .order_by(RefinementEvent.round)
    )).all()  # fmt: skip
    view = item_view(item, versions[-1] if versions else None, message.channel if message else None)
    review_by_version = {r.draft_version_id: r for r in reviews}
    view["versions"] = [
        {**(draft_view(v) or {}),
         "review": (review_by_version[v.id].rule_results if v.id in review_by_version else None)}
        for v in versions
    ]  # fmt: skip
    view["agent_runs"] = [
        {"agent": r.agent, "status": r.status, "started_at": r.started_at.isoformat(),
         "finished_at": r.finished_at.isoformat() if r.finished_at else None,
         "latency_ms": r.latency_ms, "draft_version_id": str(r.draft_version_id)
         if r.draft_version_id else None, "output": r.output_json}
        for r in runs
    ]  # fmt: skip
    view["refinements"] = [
        {"round": e.round, "feedback_type": e.feedback_type, "agents_fired": e.agents_fired}
        for e in events
    ]
    return view


class RefineRequest(BaseModel):
    feedback: str = Field(min_length=1, max_length=2000)


@router.post("/queue/{item_id}/refine")
async def refine(item_id: uuid.UUID, body: RefineRequest, user: User, deps: Deps) -> Any:
    try:
        out = await refine_item(deps, item_id, body.feedback, user_id=user.id)
    except DeskError as exc:
        raise _http(exc) from exc
    return {"round": out.round, "feedback_type": out.feedback_type,
            "agents_fired": out.agents_fired, "draft": draft_view(out.draft)}  # fmt: skip


class EditRequest(BaseModel):
    reply: str = Field(min_length=1, max_length=2000)


@router.put("/queue/{item_id}/draft")
async def edit(item_id: uuid.UUID, body: EditRequest, user: User, deps: Deps) -> Any:
    try:
        draft = await edit_draft(deps, item_id, body.reply, user_id=user.id)
    except DeskError as exc:
        raise _http(exc) from exc
    return {"draft": draft_view(draft)}


@router.post("/queue/{item_id}/approve")
async def approve(item_id: uuid.UUID, user: User, deps: Deps) -> Any:
    try:
        action_ids = await approve_item(deps, item_id, user_id=user.id)
    except DeskError as exc:
        raise _http(exc) from exc
    return {"status": "approved", "pending_action_ids": action_ids}


@router.post("/queue/{item_id}/reject")
async def reject(item_id: uuid.UUID, user: User, deps: Deps) -> Any:
    try:
        await reject_item(deps, item_id)
    except DeskError as exc:
        raise _http(exc) from exc
    return {"status": "rejected"}


class ImportRequest(BaseModel):
    review_ids: list[str] = Field(min_length=1, max_length=20)


@router.post("/queue/import-csv")
async def import_reviews(body: ImportRequest, user: User, session: Session, deps: Deps) -> Any:
    """Selected rows of the reviews CSV (already ingested, redacted) become queue items."""
    reviews = (await session.scalars(
        select(Review).where(Review.review_id.in_(body.review_ids))
    )).all()  # fmt: skip
    found = {r.review_id: r for r in reviews}
    missing = [rid for rid in body.review_ids if rid not in found]
    if missing:
        raise HTTPException(404, f"reviews not found: {', '.join(missing[:5])}")
    item_ids = []
    for rid in body.review_ids:
        intake = await intake_message(session, deps, found[rid].text_redacted, channel="csv",
                                      session_id=None)  # fmt: skip
        item_ids.extend(intake.item_ids)
    await session.commit()
    outcomes = await process_message(deps, item_ids, None)
    return {"items": [{"id": str(o.item_id), "status": o.status, "intent": o.intent}
                      for o in outcomes]}  # fmt: skip
