"""The approval gate (architecture section 10.2).

approve_action, in one database transaction:
  1. lock the action row (SELECT ... FOR UPDATE), so a double click waits, then sees it done
  2. check it is pending and that the hash the human saw matches the stored payload
  3. insert the approval row (unique per action: one approval, one action)
  4. execute it: an MCP tool call, or for promote_canonical a database write
  5. store the result id, or mark the action failed with the error
A failed action keeps its approval and can be retried; doc appends carry a dedupe key, so a
retry never writes the same entry twice.

Approving one action never touches another: the doc append, the email draft and
promote_canonical each need their own approval.
"""

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.connector.canonical import bullet_chunk_ids
from app.corpus.embed import Embedder
from app.db.models import (
    Approval,
    CanonicalExplainer,
    DraftVersion,
    FeeExplainer,
    Message,
    PendingAction,
    QueueItem,
)
from app.gate.actions import new_pending_action, payload_hash
from app.mcp.allowlist import ToolNotAllowedError
from app.mcp.client import McpClient, McpToolError
from app.mcp.execute import NotApprovedError, execute


class GateError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


async def _locked(s: AsyncSession, action_id: uuid.UUID) -> PendingAction:
    action = await s.scalar(
        select(PendingAction).where(PendingAction.id == action_id).with_for_update()
    )
    if action is None:
        raise GateError(404, "action not found")
    return action


async def promote_canonical(
    s: AsyncSession,
    action: PendingAction,
    approver_id: uuid.UUID,
    embedder: Embedder,
    now: datetime,
) -> str:
    """Make the run's fee explainer the canonical answer for its fee type. The previous
    canonical explainer for that fee type is archived. Writes only to the database.
    Each bullet is linked to the corpus chunk it came from, so replies can cite it."""
    payload = action.payload
    explainer = await s.get(FeeExplainer, uuid.UUID(payload["explainer_id"]))
    if explainer is None:
        raise GateError(409, "the fee explainer no longer exists")
    if (
        list(explainer.bullets) != payload["bullets"]
        or list(explainer.source_urls) != payload["source_urls"]
    ):
        raise GateError(409, "the fee explainer changed after this action was created")
    chunk_ids = await bullet_chunk_ids(s, explainer, embedder)
    if not any(chunk_ids):
        raise GateError(409, "no bullet of this explainer could be traced to a corpus chunk")
    await s.execute(
        update(CanonicalExplainer)
        .where(CanonicalExplainer.fee_type == explainer.fee_type, CanonicalExplainer.active)
        .values(active=False, archived_at=now)
    )
    text = "\n".join(explainer.bullets)
    [embedding] = await asyncio.to_thread(embedder.embed_documents, [text])
    canonical = CanonicalExplainer(
        fee_type=explainer.fee_type,
        explainer_id=explainer.id,
        embedding=embedding,
        approved_by=approver_id,
        active=True,
        bullet_chunk_ids=chunk_ids,
    )
    s.add(canonical)
    await s.flush()
    return str(canonical.id)


async def _run(
    s: AsyncSession,
    action: PendingAction,
    approval: Approval,
    *,
    client: McpClient,
    embedder: Embedder,
    approver_id: uuid.UUID,
    now: Callable[[], datetime],
) -> None:
    try:
        if action.kind == "promote_canonical":
            result = await promote_canonical(s, action, approver_id, embedder, now())
        else:
            result = (await execute(s, action.id, approval.id, client)).result_id
    except (McpToolError, ToolNotAllowedError, NotApprovedError) as exc:
        action.status = "failed"
        action.error = str(exc)[:500]
        if isinstance(exc, McpToolError) and exc.outcome_unknown:
            action.error += " (outcome unknown: check the target before retrying)"
        return
    action.status = "executed"
    action.result_id = result
    action.error = None
    action.executed_at = now()


async def approve_action(
    s: AsyncSession,
    action_id: uuid.UUID,
    shown_hash: str,
    *,
    approver_id: uuid.UUID,
    client: McpClient,
    embedder: Embedder,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PendingAction:
    action = await _locked(s, action_id)
    if action.status != "pending":
        raise GateError(409, f"action is already {action.status}")
    if shown_hash != action.payload_hash or payload_hash(action.payload) != action.payload_hash:
        raise GateError(409, "the payload changed since it was shown; reload and review it again")
    approval = Approval(action_id=action.id, payload_hash=shown_hash, approver_id=approver_id)
    s.add(approval)
    action.status = "approved"
    await s.flush()
    await _run(
        s, action, approval, client=client, embedder=embedder, approver_id=approver_id, now=now
    )
    await s.commit()
    return action


async def retry_action(
    s: AsyncSession,
    action_id: uuid.UUID,
    *,
    client: McpClient,
    embedder: Embedder,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> PendingAction:
    """Run a failed action again under its existing approval."""
    action = await _locked(s, action_id)
    if action.status != "failed":
        raise GateError(409, f"only a failed action can be retried (this one is {action.status})")
    approval = await s.scalar(select(Approval).where(Approval.action_id == action.id))
    if approval is None:
        raise GateError(409, "action has no approval")
    await _run(
        s,
        action,
        approval,
        client=client,
        embedder=embedder,
        approver_id=approval.approver_id,
        now=now,
    )
    await s.commit()
    return action


async def reject_action(s: AsyncSession, action_id: uuid.UUID) -> PendingAction:
    action = await _locked(s, action_id)
    if action.status != "pending":
        raise GateError(409, f"action is already {action.status}")
    action.status = "rejected"
    await s.commit()
    return action


async def combined_reply(s: AsyncSession, message_id: uuid.UUID, *, today: str) -> PendingAction:
    """One email draft holding every approved answer of a multi-question message, in order.
    Only once every part is approved or rejected. It needs its own approval."""
    message = await s.scalar(select(Message).where(Message.id == message_id).with_for_update())
    if message is None:
        raise GateError(404, "message not found")
    items = (
        await s.scalars(
            select(QueueItem)
            .where(QueueItem.message_id == message.id)
            .order_by(QueueItem.part_index)
        )
    ).all()
    open_items = [i for i in items if i.status not in ("approved", "rejected")]
    if open_items:
        raise GateError(409, f"{len(open_items)} part(s) are not approved or rejected yet")
    approved = [i for i in items if i.status == "approved"]
    if not approved:
        raise GateError(409, "no part of this message was approved")
    existing = await s.scalar(
        select(PendingAction).where(
            PendingAction.origin == "message",
            PendingAction.origin_id == message.id,
            PendingAction.status.in_(("pending", "approved", "executed")),
        )
    )
    if existing is not None:
        raise GateError(409, "this message already has a combined reply")
    blocks = []
    for n, item in enumerate(approved, start=1):
        draft = await s.scalar(
            select(DraftVersion)
            .where(DraftVersion.item_id == item.id)
            .order_by(DraftVersion.version.desc())
            .limit(1)
        )
        assert draft is not None
        blocks.append(f"{n}. {item.text_redacted}\n{draft.reply_redacted}")
    payload = {
        "subject": f"Reply to your questions — {today}",
        "body": "\n\n".join(blocks),
        "to": None,  # no recipient: the support agent adds it in Gmail
        "item_ids": [str(i.id) for i in approved],
    }
    action = new_pending_action(
        s,
        kind="email_draft",
        origin="message",
        origin_id=message.id,
        target="gmail_drafts",
        payload=payload,
    )
    await s.commit()
    return action
