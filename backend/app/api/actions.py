"""Approval gate API (architecture section 13).

GET  /actions/pending                 what is waiting: kind, target, full payload and its hash
GET  /actions/{id}                    one action, with its approval and result
POST /actions/{id}/approve            approve and execute exactly this action; needs payload_hash
POST /actions/{id}/reject             reject exactly this action
POST /actions/{id}/retry              run a failed action again under its approval
POST /messages/{id}/combined-reply    one email draft with every approved answer of a message
"""

import uuid
from datetime import date
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.auth import current_user
from app.config import get_settings
from app.corpus.embed import Embedder
from app.db.models import Approval, AppUser, PendingAction
from app.db.session import get_session
from app.gate.service import (
    GateError,
    approve_action,
    combined_reply,
    reject_action,
    retry_action,
)
from app.mcp.client import McpClient, StdioMcpClient

router = APIRouter(tags=["gate"])

User = Annotated[AppUser, Depends(current_user)]
Session = Annotated[AsyncSession, Depends(get_session)]


@lru_cache
def get_mcp_client() -> McpClient:
    return StdioMcpClient(get_settings())


def get_embedder() -> Embedder:
    from app.orchestrator.deps import get_desk_deps

    return get_desk_deps().embedder


Client = Annotated[McpClient, Depends(get_mcp_client)]
EmbedderDep = Annotated[Embedder, Depends(get_embedder)]


def _http(exc: GateError) -> HTTPException:
    return HTTPException(exc.status, str(exc))


def action_view(a: PendingAction, approval: Approval | None = None) -> dict[str, Any]:
    return {
        "id": str(a.id),
        "kind": a.kind,
        "origin": a.origin,
        "origin_id": str(a.origin_id),
        "target": a.target,
        "payload": a.payload,
        "payload_hash": a.payload_hash,
        "status": a.status,
        "result_id": a.result_id,
        "error": a.error,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "executed_at": a.executed_at.isoformat() if a.executed_at else None,
        "approval": (
            {
                "approver_id": str(approval.approver_id),
                "approved_at": approval.approved_at.isoformat() if approval.approved_at else None,
            }
            if approval
            else None
        ),
    }


@router.get("/actions/pending")
async def pending_actions(user: User, session: Session) -> Any:
    rows = (
        await session.scalars(
            select(PendingAction)
            .where(PendingAction.status.in_(("pending", "failed")))
            .order_by(PendingAction.created_at)
        )
    ).all()
    return {"actions": [action_view(a) for a in rows]}


@router.get("/actions/{action_id}")
async def get_action(action_id: uuid.UUID, user: User, session: Session) -> Any:
    action = await session.get(PendingAction, action_id)
    if action is None:
        raise HTTPException(404, "action not found")
    approval = await session.scalar(select(Approval).where(Approval.action_id == action.id))
    return action_view(action, approval)


class ApproveRequest(BaseModel):
    payload_hash: str = Field(min_length=64, max_length=64)


@router.post("/actions/{action_id}/approve")
async def approve(
    action_id: uuid.UUID,
    body: ApproveRequest,
    user: User,
    session: Session,
    client: Client,
    embedder: EmbedderDep,
) -> Any:
    try:
        action = await approve_action(
            session,
            action_id,
            body.payload_hash,
            approver_id=user.id,
            client=client,
            embedder=embedder,
        )
    except GateError as exc:
        await session.rollback()
        raise _http(exc) from exc
    return action_view(action)


@router.post("/actions/{action_id}/reject")
async def reject(action_id: uuid.UUID, user: User, session: Session) -> Any:
    try:
        return action_view(await reject_action(session, action_id))
    except GateError as exc:
        await session.rollback()
        raise _http(exc) from exc


@router.post("/actions/{action_id}/retry")
async def retry(
    action_id: uuid.UUID, user: User, session: Session, client: Client, embedder: EmbedderDep
) -> Any:
    try:
        return action_view(await retry_action(session, action_id, client=client, embedder=embedder))
    except GateError as exc:
        await session.rollback()
        raise _http(exc) from exc


@router.post("/messages/{message_id}/combined-reply")
async def combined(message_id: uuid.UUID, user: User, session: Session) -> Any:
    try:
        return action_view(
            await combined_reply(session, message_id, today=date.today().isoformat())
        )
    except GateError as exc:
        await session.rollback()
        raise _http(exc) from exc
