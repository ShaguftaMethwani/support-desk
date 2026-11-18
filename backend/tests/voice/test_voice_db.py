"""A confirmed voice transcript through the desk, against real Postgres. Skipped when no
database is reachable."""

import pytest
from sqlalchemy import select

from app.db.models import Message, QueueItem
from app.guard.redactor import find_pii
from app.orchestrator.desk import intake_message
from tests.agents.test_desk_db import Tracker, make_deps, make_llm, tracker  # noqa: F401

pytestmark = pytest.mark.db


async def test_a_confirmed_transcript_keeps_its_pii_flags(tracker: Tracker) -> None:  # noqa: F811
    deps = make_deps(tracker, make_llm())
    confirmed = "My pan is [PAN_REDACTED] is my redemption delayed?"
    async with tracker.sessions() as s:
        intake = await intake_message(s, deps, confirmed, channel="voice", session_id=None)
        await s.commit()
    tracker.messages.append(intake.message_id)
    assert intake.pii_flags == ["pan"]
    async with tracker.sessions() as s:
        message = await s.get(Message, intake.message_id)
        assert message is not None
        assert message.channel == "voice" and message.pii_flags == ["pan"]
        [item] = (
            await s.scalars(select(QueueItem).where(QueueItem.message_id == message.id))
        ).all()
        assert find_pii(item.text_redacted) == []


async def test_an_edited_transcript_is_guarded_again(tracker: Tracker) -> None:  # noqa: F811
    # The user may type PII into the transcript while correcting it.
    deps = make_deps(tracker, make_llm())
    async with tracker.sessions() as s:
        intake = await intake_message(
            s, deps, "Call me on 9876543210 about my SIP", channel="voice", session_id=None
        )
        await s.commit()
    tracker.messages.append(intake.message_id)
    async with tracker.sessions() as s:
        message = await s.get(Message, intake.message_id)
        assert message is not None
        assert "9876543210" not in message.text_redacted and message.pii_flags == ["phone"]
