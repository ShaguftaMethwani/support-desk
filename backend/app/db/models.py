"""Database tables (architecture section 12).

Naming rule: every column that can hold user-originated or user-derived free text ends in
`_redacted`. Phase 1 adds a write guard that rejects PII in any such column, so raw text
can't be stored even by mistake.
"""

import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

EMBED_DIM = 384  # BAAI/bge-small-en-v1.5
REDACTED_SUFFIX = "_redacted"


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _json_list() -> Mapped[list[Any]]:
    return mapped_column(JSONB, default=list, server_default="[]")


# --- Users and sessions ---------------------------------------------------------------


class AppUser(Base):
    __tablename__ = "app_user"

    id: Mapped[uuid.UUID] = _uuid_pk()
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(16), default="agent", server_default="agent")
    created_at: Mapped[datetime] = _created_at()


class ChatSession(Base):
    __tablename__ = "chat_session"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    last_scheme: Mapped[str | None] = mapped_column(String(64))
    advice_refused: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- Corpus ---------------------------------------------------------------------------


class CorpusSource(Base):
    __tablename__ = "corpus_source"

    id: Mapped[uuid.UUID] = _uuid_pk()
    key: Mapped[str] = mapped_column(String(64), unique=True)  # id in corpus/sources.yaml
    url: Mapped[str] = mapped_column(Text, unique=True)
    publisher: Mapped[str] = mapped_column(String(32))  # hdfc_mf | sebi | amfi | indmoney_help
    doc_type: Mapped[str] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(Text)
    schemes: Mapped[list[Any]] = _json_list()
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()


class CorpusChunk(Base):
    __tablename__ = "corpus_chunk"
    __table_args__ = (Index("ix_corpus_chunk_tsv", "tsv", postgresql_using="gin"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("corpus_source.id", ondelete="CASCADE"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    section: Mapped[str | None] = mapped_column(Text)
    scheme: Mapped[str | None] = mapped_column(String(64), index=True)
    page: Mapped[int | None] = mapped_column(Integer)
    token_count: Mapped[int | None] = mapped_column(Integer)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('english', text)", persisted=True)
    )


# --- Reviews and the weekly loop ------------------------------------------------------


class Review(Base):
    __tablename__ = "review"

    review_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    source: Mapped[str] = mapped_column(String(16))  # play_store | app_store
    date: Mapped[date] = mapped_column(Date, index=True)
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    text_redacted: Mapped[str] = mapped_column(Text)
    pii_flags: Mapped[list[Any]] = _json_list()
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    ingested_at: Mapped[datetime] = _created_at()


class WeeklyRun(Base):
    __tablename__ = "weekly_run"

    id: Mapped[uuid.UUID] = _uuid_pk()
    window_start: Mapped[date] = mapped_column(Date)
    window_end: Mapped[date] = mapped_column(Date)
    # pending | in_progress | awaiting_approval | completed | failed
    status: Mapped[str] = mapped_column(String(24), default="pending", server_default="pending")
    stage_log: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    triggered_by: Mapped[str] = mapped_column(String(32))  # cron | manual | eval
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = _created_at()
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Theme(Base):
    __tablename__ = "theme"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("weekly_run.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    count: Mapped[int] = mapped_column(Integer)
    rank: Mapped[int | None] = mapped_column(SmallInteger)
    centroid: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    review_ids: Mapped[list[Any]] = _json_list()
    question_ids: Mapped[list[Any]] = _json_list()
    from_support_queue: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    # For a support-queue theme: the surfaced question cluster it reports.
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("question_cluster.id", ondelete="SET NULL"), index=True
    )


class Pulse(Base):
    __tablename__ = "pulse"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("weekly_run.id", ondelete="CASCADE"), unique=True
    )
    text_redacted: Mapped[str] = mapped_column(Text)
    word_count: Mapped[int] = mapped_column(Integer)
    quote_review_ids: Mapped[list[Any]] = _json_list()
    created_at: Mapped[datetime] = _created_at()


class FeeExplainer(Base):
    __tablename__ = "fee_explainer"

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("weekly_run.id", ondelete="CASCADE"), unique=True
    )
    fee_type: Mapped[str] = mapped_column(String(64))
    bullets: Mapped[list[Any]] = _json_list()
    source_urls: Mapped[list[Any]] = _json_list()
    last_checked: Mapped[date] = mapped_column(Date)
    evidence_review_ids: Mapped[list[Any]] = _json_list()
    # The corpus chunk each bullet came from, in bullet order (empty for runs before Phase 6).
    chunk_ids: Mapped[list[Any]] = _json_list()
    created_at: Mapped[datetime] = _created_at()


class CanonicalExplainer(Base):
    __tablename__ = "canonical_explainer"

    id: Mapped[uuid.UUID] = _uuid_pk()
    fee_type: Mapped[str] = mapped_column(String(64), index=True)
    explainer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("fee_explainer.id"))
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    approved_at: Mapped[datetime] = _created_at()
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The corpus chunk behind each of the explainer's bullets, so a reply can cite it.
    bullet_chunk_ids: Mapped[list[Any]] = _json_list()
    # Queue items whose reply was built from this explainer.
    reuse_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


# --- Response loop --------------------------------------------------------------------


class Message(Base):
    __tablename__ = "message"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("chat_session.id", ondelete="SET NULL"), index=True
    )
    channel: Mapped[str] = mapped_column(String(8))  # text | voice | csv
    text_redacted: Mapped[str] = mapped_column(Text)
    pii_flags: Mapped[list[Any]] = _json_list()
    split_method: Mapped[str] = mapped_column(String(8))  # rules | model | single
    parts_dropped: Mapped[list[Any]] = _json_list()
    created_at: Mapped[datetime] = _created_at()


class QuestionCluster(Base):
    __tablename__ = "question_cluster"

    id: Mapped[uuid.UUID] = _uuid_pk()
    centroid: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    first_seen: Mapped[datetime] = _created_at()
    last_seen: Mapped[datetime] = _created_at()
    surfaced: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    surfaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class QueueItem(Base):
    __tablename__ = "queue_item"
    __table_args__ = (UniqueConstraint("message_id", "part_index"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    message_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("message.id", ondelete="CASCADE"))
    part_index: Mapped[int] = mapped_column(SmallInteger)
    text_redacted: Mapped[str] = mapped_column(Text)
    shared_slots: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default="{}")
    intent: Mapped[str | None] = mapped_column(String(32))
    intent_confidence: Mapped[float | None] = mapped_column(Float)
    # new | drafting | awaiting_human | blocked | approved | rejected | unavailable
    status: Mapped[str] = mapped_column(String(24), default="new", server_default="new", index=True)
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("question_cluster.id", ondelete="SET NULL"), index=True
    )
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBED_DIM))
    created_at: Mapped[datetime] = _created_at()


class DraftVersion(Base):
    __tablename__ = "draft_version"
    __table_args__ = (UniqueConstraint("item_id", "version"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("queue_item.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(SmallInteger)
    reply_redacted: Mapped[str] = mapped_column(Text)
    citation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    last_checked: Mapped[date | None] = mapped_column(Date)
    # corpus | canonical_explainer | refusal | not_covered | clarification
    source_type: Mapped[str] = mapped_column(String(24))
    # Set when source_type is canonical_explainer: the explainer the reply was built from.
    canonical_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("canonical_explainer.id", ondelete="SET NULL")
    )
    routing: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    author: Mapped[str] = mapped_column(String(8))  # system | human
    author_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("app_user.id"))
    model_tag: Mapped[str | None] = mapped_column(String(16))  # e.g. "fallback"
    # pending | passed | rework | blocked
    review_status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending"
    )
    created_at: Mapped[datetime] = _created_at()


class AgentRun(Base):
    __tablename__ = "agent_run"

    id: Mapped[uuid.UUID] = _uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("queue_item.id", ondelete="CASCADE"), index=True
    )
    draft_version_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("draft_version.id", ondelete="CASCADE")
    )
    agent: Mapped[str] = mapped_column(String(16))  # facts | pattern | compliance | ...
    status: Mapped[str] = mapped_column(String(16))  # ok | error | cached
    output_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int | None] = mapped_column(Integer)


class ReviewResult(Base):
    __tablename__ = "review_result"

    id: Mapped[uuid.UUID] = _uuid_pk()
    draft_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("draft_version.id", ondelete="CASCADE"), index=True
    )
    attempt: Mapped[int] = mapped_column(SmallInteger)
    passed: Mapped[bool] = mapped_column(Boolean)
    rule_results: Mapped[list[Any]] = _json_list()
    created_at: Mapped[datetime] = _created_at()


class RefinementEvent(Base):
    __tablename__ = "refinement_event"
    __table_args__ = (UniqueConstraint("item_id", "round"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("queue_item.id", ondelete="CASCADE"))
    round: Mapped[int] = mapped_column(SmallInteger)
    feedback_type: Mapped[str] = mapped_column(String(24))
    agents_fired: Mapped[list[Any]] = _json_list()
    created_at: Mapped[datetime] = _created_at()


# --- Approval gate --------------------------------------------------------------------


class PendingAction(Base):
    __tablename__ = "pending_action"

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(24))  # doc_append | email_draft | promote_canonical
    origin: Mapped[str] = mapped_column(String(16))  # weekly_run | queue_item | message
    origin_id: Mapped[uuid.UUID] = mapped_column(index=True)
    target: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    payload_hash: Mapped[str] = mapped_column(String(64))
    # pending | approved | executed | rejected | failed
    status: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending", index=True
    )
    result_id: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Approval(Base):
    __tablename__ = "approval"

    id: Mapped[uuid.UUID] = _uuid_pk()
    action_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pending_action.id"), unique=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    approver_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id"))
    approved_at: Mapped[datetime] = _created_at()


# --- LLM cache ------------------------------------------------------------------------


class LlmCache(Base):
    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    model: Mapped[str] = mapped_column(String(64))
    response_text: Mapped[str] = mapped_column(Text)
    prompt_tokens: Mapped[int] = mapped_column(Integer)
    completion_tokens: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = _created_at()


def redacted_columns() -> list[str]:
    """All `table.column` names that hold redacted free text (guarded in Phase 1)."""
    return sorted(
        f"{table.name}.{column.name}"
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if column.name.endswith(REDACTED_SUFFIX)
    )


# Refuse PII in any *_redacted column on every write (Phase 1 backstop).
from app.guard.db_guard import install_db_write_guard  # noqa: E402

install_db_write_guard(Base)
