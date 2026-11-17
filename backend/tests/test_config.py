import pytest

from app.config import Settings, Tier
from app.llm.tasks import TASK_TIERS, UnknownTaskError, tier_for


def test_defaults_and_tier_mapping(settings: Settings) -> None:
    assert settings.model_for_tier(Tier.SMALL) == "openai/gpt-oss-20b"
    assert settings.model_for_tier(Tier.LARGE) == "openai/gpt-oss-120b"
    assert settings.limits_for_tier(Tier.SMALL) == (30, 8000)
    assert settings.limits_for_tier(Tier.LARGE) == (30, 8000)
    assert settings.reasoning_effort == "low"


def test_empty_reasoning_effort_means_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REASONING_EFFORT", "")
    assert Settings(_env_file=None).reasoning_effort is None  # type: ignore[call-arg]


def test_env_overrides_model_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_LARGE", "some-new-model")
    monkeypatch.setenv("LARGE_TPM", "5000")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.model_for_tier(Tier.LARGE) == "some-new-model"
    assert s.limits_for_tier(Tier.LARGE) == (30, 5000)


def test_secrets_hidden_in_repr(settings: Settings) -> None:
    assert "test-key" not in repr(settings)


def test_every_task_has_a_tier() -> None:
    assert TASK_TIERS
    assert tier_for("intent") is Tier.SMALL
    assert tier_for("orchestrate") is Tier.LARGE


def test_unknown_task_raises() -> None:
    with pytest.raises(UnknownTaskError):
        tier_for("write_a_poem")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "postgresql://u:p@ep-x-pooler.neon.tech/db?sslmode=require&channel_binding=require",
            "postgresql+asyncpg://u:p@ep-x-pooler.neon.tech/db?ssl=require",
        ),
        ("postgres://u:p@host/db", "postgresql+asyncpg://u:p@host/db"),
        (
            "postgresql+asyncpg://u:p@host/db?ssl=require",
            "postgresql+asyncpg://u:p@host/db?ssl=require",
        ),
        (
            "postgresql+asyncpg://u:p@/db?host=/tmp/sock",
            "postgresql+asyncpg://u:p@/db?host=%2Ftmp%2Fsock",
        ),
    ],
)
def test_database_url_is_normalized_for_asyncpg(raw: str, expected: str) -> None:
    assert Settings(_env_file=None, database_url=raw).database_url == expected  # type: ignore[call-arg]
