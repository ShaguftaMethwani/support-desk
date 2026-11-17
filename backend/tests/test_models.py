from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.db.base import Base
from app.db.models import REDACTED_SUFFIX, redacted_columns

EXPECTED_TABLES = {
    "agent_run",
    "app_user",
    "approval",
    "canonical_explainer",
    "chat_session",
    "corpus_chunk",
    "corpus_source",
    "draft_version",
    "fee_explainer",
    "llm_cache",
    "message",
    "pending_action",
    "pulse",
    "queue_item",
    "question_cluster",
    "refinement_event",
    "review",
    "review_result",
    "theme",
    "weekly_run",
}


def test_all_architecture_tables_exist() -> None:
    assert set(Base.metadata.tables) == EXPECTED_TABLES


def test_user_text_columns_follow_redacted_naming_rule() -> None:
    assert redacted_columns() == [
        "draft_version.reply_redacted",
        "message.text_redacted",
        "pulse.text_redacted",
        "queue_item.text_redacted",
        "review.text_redacted",
    ]
    assert all(c.endswith(REDACTED_SUFFIX) for c in redacted_columns())


def test_no_unguarded_user_text_columns() -> None:
    """Tables that hold user text must not have a second free-text column without the suffix."""
    user_text_tables = {"message", "queue_item", "review", "draft_version", "pulse"}
    for name in user_text_tables:
        table = Base.metadata.tables[name]
        plain_text_cols = [
            c.name
            for c in table.columns
            if c.type.__class__.__name__ == "Text" and not c.name.endswith(REDACTED_SUFFIX)
        ]
        assert plain_text_cols == [], f"{name} has unguarded text columns {plain_text_cols}"


def test_ddl_compiles_for_postgres() -> None:
    dialect = postgresql.dialect()  # type: ignore[no-untyped-call]
    for table in Base.metadata.sorted_tables:
        assert "CREATE TABLE" in str(CreateTable(table).compile(dialect=dialect))


def test_approval_is_one_per_action() -> None:
    action_id = Base.metadata.tables["approval"].c.action_id
    assert action_id.unique
