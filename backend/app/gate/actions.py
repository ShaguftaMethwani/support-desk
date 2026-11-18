"""Pending actions (architecture section 10.1).

Creating an action only records what *would* be written. Nothing reaches Google Docs, Gmail
or the canonical explainer table until a human approves that one action through the gate
(Phase 5), which re-computes `payload_hash` and rejects the approval if it differs.
"""

import hashlib
import json
import uuid
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PendingAction

ActionKind = Literal["doc_append", "email_draft", "promote_canonical"]
Origin = Literal["weekly_run", "queue_item", "message"]


def canonical_json(payload: dict[str, Any]) -> str:
    """One fixed serialisation: sorted keys, no spaces, UTF-8 kept as is."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def new_pending_action(
    session: AsyncSession,
    *,
    kind: ActionKind,
    origin: Origin,
    origin_id: uuid.UUID,
    target: str,
    payload: dict[str, Any],
) -> PendingAction:
    # Round-trip through JSON so the stored payload is exactly what was hashed.
    stored = json.loads(canonical_json(payload))
    action = PendingAction(
        kind=kind,
        origin=origin,
        origin_id=origin_id,
        target=target,
        payload=stored,
        payload_hash=payload_hash(stored),
        status="pending",
    )
    session.add(action)
    return action
