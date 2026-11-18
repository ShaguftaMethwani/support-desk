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
    # Voice (architecture section 11). Groq Whisper; clips are capped at 30 seconds.
    whisper_model: str = "whisper-large-v3-turbo"
    whisper_rpm: int = 20  # Groq free tier
    whisper_timeout_seconds: float = 30.0
    voice_max_seconds: float = 30.0
    # A size cap checked while the upload streams in: 30 s of 48 kHz stereo WAV is 5.8 MB;
    # a browser's webm/opus clip of 30 s is about 120 KB.
    voice_max_bytes: int = 6_000_000
    # rapidfuzz ratio from which a misheard scheme name ("big cap") gets a suggestion.
    voice_scheme_min_score: float = 70.0
    llm_cache_enabled: bool = True

    # Below this cosine similarity, the Facts Agent answers "not covered" without a model call.
    # Calibrated on the retrieval test set: answerable questions >= 0.72, off-topic <= 0.60.
    retrieval_min_similarity: float = 0.65

    # Domains a citation or source link may point to ("public sources only").
    source_domains: list[str] = [
        "hdfcfund.com",
        "sebi.gov.in",
        "amfiindia.com",
        "indmoney.com",
    ]

    weekly_trigger_token: SecretStr | None = None

    # Response loop (architecture section 7).
    intent_min_confidence: float = 0.6  # below this the intent becomes "unclear"
    # After an advice refusal in a session, a model "advice" label counts from this confidence.
    sticky_advice_min_confidence: float = 0.3
    chat_session_ttl_minutes: int = 120
    # Queue items drafted at once. Each holds up to 3 database connections while it runs.
    desk_parallel_items: int = 3
    # Pattern Agent. Calibrated on the real reviews (bge-small): related complaints score
    # 0.74 to 0.85 against a question, fact questions at most 0.67, off-topic text about 0.5.
    pattern_similarity: float = 0.74
    # Theme centroids sit closer to everything (they average many reviews): a crash question
    # scores 0.785 to the crash theme, unrelated questions 0.75 to 0.76.
    theme_match_similarity: float = 0.77
    pattern_window_days: int = 28
    recurring_min: int = 5  # similar items in the window
    rising_min: int = 3  # this week's items, and at least double the previous week's
    question_cluster_similarity: float = 0.85
    # Canonical explainers (architecture section 9.2): question to explainer, cosine (bge-small,
    # measured on the November 2026 expense ratio explainer). Related questions score 0.52 to
    # 0.81; unrelated ones without a scheme at most 0.57. Paraphrases such as "why is my
    # regular plan charging more" score low, but they name the fee type, so they match on that.
    # A fee_canonical question naming no fee type uses the nearest explainer from this:
    canonical_fee_similarity: float = 0.68
    # a factual question or complaint (no scheme, no fee type named) needs this,
    canonical_similarity: float = 0.82
    # or this when it names the explainer's fee type.
    canonical_fee_type_similarity: float = 0.55
    # Weekly loop (architecture section 8).
    reviews_csv_path: Path = REPO_ROOT / "data" / "reviews_sample.csv"
    weekly_window_weeks: int = 12  # the review sample covers the last 12 weeks
    weekly_min_themes: int = 3  # KMeans tries k = min .. max; the pulse needs a top 3
    weekly_max_themes: int = 5
    # Reviews shorter than this are counted but not clustered: "nice app" carries no theme.
    theme_min_words: int = 8
    weekly_cluster_seed: int = 42
    # Two reviews this similar (cosine) are near-duplicates; only the first is kept.
    review_dedupe_similarity: float = 0.95
    # A fee type needs at least this many reviews before it counts as "fee confusion".
    fee_min_reviews: int = 3
    # Resume runs left in_progress when the API starts (off in unit tests).
    weekly_resume_on_startup: bool = True

    google_client_id: str | None = None
    google_client_secret: SecretStr | None = None
    google_refresh_token: SecretStr | None = None
    google_doc_id: str | None = None
    # The vendored MCP server (mcp_server/), started over stdio for each tool call.
    # By default `node dist/index.js` in mcp_server_dir; set the command to override.
    mcp_server_command: str | None = None
    mcp_server_dir: Path = REPO_ROOT / "mcp_server"
    mcp_timeout_seconds: float = 30.0

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
