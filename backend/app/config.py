"""Application settings, loaded from environment variables and the repo-root .env file.

Model names and rate limits live here, never in code, because Groq changes its model list often.
"""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Tier(StrEnum):
    """Model size tier. Tasks map to a tier, and the tier maps to a configured model."""

    SMALL = "small"
    LARGE = "large"


def normalize_database_url(url: str) -> str:
    """Make a standard Postgres URL (for example, copied from Neon) work with asyncpg.

    - postgres:// and postgresql:// become postgresql+asyncpg://
    - sslmode=... becomes ssl=... (asyncpg's name for it)
    - channel_binding is dropped, because asyncpg doesn't accept it
    """
    parts = urlsplit(url)
    scheme = parts.scheme
    if scheme in ("postgres", "postgresql"):
        scheme = "postgresql+asyncpg"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        if key == "channel_binding":
            continue
        query.append(("ssl" if key == "sslmode" else key, value))
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),
    )

    app_env: str = "local"
    log_level: str = "INFO"
    log_json: bool = True
    cors_origins: list[str] = ["http://localhost:5173"]

    database_url: str = "postgresql+asyncpg://support:support@localhost:5432/support_desk"

    groq_api_key: SecretStr | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    model_small: str = "openai/gpt-oss-20b"
    model_large: str = "openai/gpt-oss-120b"
    model_stt: str = "whisper-large-v3-turbo"
    # gpt-oss models reason before answering. "low" keeps that short, so replies arrive
    # within small token budgets. Set REASONING_EFFORT= (empty) for non-reasoning models.
    reasoning_effort: str | None = "low"
    # Reasoning tokens count toward max_tokens, so no call gets a smaller budget than this.
    min_completion_tokens: int = 64

    # Free-tier limits seen on this project's key (Nov 2026): 1,000 requests per day and
    # 8,000 tokens per minute per model. Requests per minute is Groq's general default.
    small_rpm: int = 30
    small_tpm: int = 8000
    large_rpm: int = 30
    large_tpm: int = 8000
    llm_max_retries: int = 3
    llm_timeout_seconds: float = 30.0
    llm_cache_enabled: bool = True

    # Domains a citation or source link may point to ("public sources only").
    source_domains: list[str] = [
        "hdfcfund.com",
        "sebi.gov.in",
        "amfiindia.com",
        "indmoney.com",
    ]

    weekly_trigger_token: SecretStr | None = None

    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_refresh_token: SecretStr | None = None
    google_doc_id: str | None = None
    mcp_server_command: str | None = None

    @field_validator("reasoning_effort", mode="before")
    @classmethod
    def _empty_means_none(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("database_url")
    @classmethod
    def _normalize_database_url(cls, value: str) -> str:
        return normalize_database_url(value)

    def model_for_tier(self, tier: Tier) -> str:
        return self.model_small if tier is Tier.SMALL else self.model_large

    def limits_for_tier(self, tier: Tier) -> tuple[int, int]:
        """Return (requests per minute, tokens per minute) for a tier."""
        if tier is Tier.SMALL:
            return self.small_rpm, self.small_tpm
        return self.large_rpm, self.large_tpm


@lru_cache
def get_settings() -> Settings:
    return Settings()
