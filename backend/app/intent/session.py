"""Chat session memory (architecture section 7.3).

A session belongs to one user and holds only the last resolved scheme slug and the sticky
`advice_refused` flag: no message text. It expires after a period of inactivity; an expired
or unknown session id starts a new session.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatSession


@dataclass(frozen=True)
class SessionState:
    """A snapshot taken before a message is processed. Parts of one message all see the same
    snapshot, so an advice refusal in one part doesn't change how the others are handled."""

    id: uuid.UUID
    last_scheme: str | None
    advice_refused: bool


async def open_session(
    s: AsyncSession,
    session_id: uuid.UUID | None,
    user_id: uuid.UUID,
    *,
    ttl: timedelta,
    now: datetime | None = None,
) -> ChatSession:
    now = now or datetime.now(UTC)
    if session_id is not None:
        existing = await s.get(ChatSession, session_id)
        if existing is not None and existing.user_id == user_id and existing.expires_at > now:
            existing.expires_at = now + ttl
            return existing
    session = ChatSession(user_id=user_id, expires_at=now + ttl, advice_refused=False)
    s.add(session)
    await s.flush()
    return session


def snapshot(session: ChatSession) -> SessionState:
    return SessionState(session.id, session.last_scheme, bool(session.advice_refused))


def remember(session: ChatSession, *, schemes: list[str | None], advice_refused: bool) -> None:
    """After a message: the last scheme named, in order, and the sticky refusal flag."""
    named = [s for s in schemes if s]
    if named:
        session.last_scheme = named[-1]
    if advice_refused:
        session.advice_refused = True
