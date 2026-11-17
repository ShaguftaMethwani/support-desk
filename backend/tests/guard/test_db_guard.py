"""The write guard fires before any SQL runs, so an empty SQLite engine is enough here.
The Postgres version of this check is in tests/test_db.py."""

import pytest
from sqlalchemy import create_engine, insert, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.db.models import DraftVersion, Message, Review
from app.guard.db_guard import UnredactedPIIError


@pytest.fixture
def session() -> Session:
    return Session(create_engine("sqlite://"))


def test_orm_add_with_pan_is_refused(session: Session) -> None:
    session.add(
        Message(channel="text", text_redacted="my PAN is ABCPD1234F", split_method="single")
    )
    with pytest.raises(UnredactedPIIError, match=r"\(pan\) in message.text_redacted"):
        session.flush()


def test_error_message_never_contains_the_value(session: Session) -> None:
    session.add(
        Review(review_id="r1", source="play_store", date=None, text_redacted="call 9876543210")
    )
    with pytest.raises(UnredactedPIIError) as exc:
        session.flush()
    assert "9876543210" not in str(exc.value)


def test_draft_reply_column_is_guarded(session: Session) -> None:
    session.add(
        DraftVersion(
            item_id=None,
            version=1,
            reply_redacted="OTP is 482913",
            source_type="corpus",
            author="human",
        )
    )
    with pytest.raises(UnredactedPIIError):
        session.flush()


@pytest.mark.parametrize(
    "statement",
    [
        insert(Message).values(channel="text", text_redacted="call 9876543210", split_method="x"),
        update(Message).values(text_redacted="mail a.b@gmail.com"),
    ],
)
def test_orm_statements_are_refused(session: Session, statement: object) -> None:
    with pytest.raises(UnredactedPIIError):
        session.execute(statement)  # type: ignore[call-overload]


def test_bulk_insert_is_refused(session: Session) -> None:
    rows = [
        {"channel": "text", "text_redacted": "clean", "split_method": "single"},
        {"channel": "text", "text_redacted": "PAN ABCPD1234F", "split_method": "single"},
    ]
    with pytest.raises(UnredactedPIIError):
        session.execute(insert(Message), rows)


def test_clean_text_passes_the_guard(session: Session) -> None:
    session.add(Message(channel="text", text_redacted="my PAN is [PAN_REDACTED]", split_method="x"))
    # Our guard lets it through; SQLite then fails only because the table doesn't exist.
    with pytest.raises(OperationalError):
        session.flush()
