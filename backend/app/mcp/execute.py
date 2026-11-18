"""execute(): the only code path from this backend to an MCP tool (architecture section 10.2).

It re-reads the approval from the database and refuses unless that approval belongs to this
action, matches the payload hash shown at approval time, and the stored payload still hashes
to it. Then it applies the allow-list (in the client) and calls the tool.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Approval, PendingAction
from app.gate.actions import payload_hash
from app.mcp.client import McpClient, McpToolError
from app.mcp.mapping import result_id, to_tool_input

UNSET_DOC_TARGET = "google_doc_unset"


class NotApprovedError(Exception):
    pass


@dataclass(frozen=True)
class Executed:
    result_id: str
    tool: str
    skipped: bool  # a doc entry that a previous attempt already wrote


async def execute(
    s: AsyncSession, action_id: uuid.UUID, approval_id: uuid.UUID, client: McpClient
) -> Executed:
    action = await s.get(PendingAction, action_id)
    approval = await s.get(Approval, approval_id)
    if action is None or approval is None or approval.action_id != action.id:
        raise NotApprovedError("no approval for this action")
    if approval.payload_hash != action.payload_hash:
        raise NotApprovedError("the approval was for a different payload")
    if payload_hash(action.payload) != action.payload_hash:
        raise NotApprovedError("the stored payload no longer matches its hash")
    if action.status not in ("approved", "failed"):
        raise NotApprovedError(f"action is {action.status}")
    if action.kind == "doc_append" and action.target == UNSET_DOC_TARGET:
        # Created before GOOGLE_DOC_ID was configured; the target is part of what was approved,
        # so it is never filled in afterwards. Reject this action and use a newer one.
        raise McpToolError("NO_TARGET", "this action was created without a Google Doc id")
    tool, arguments = to_tool_input(action)
    data = await client.call(tool, arguments)
    return Executed(result_id(action.kind, data), tool, bool(data.get("skipped")))
