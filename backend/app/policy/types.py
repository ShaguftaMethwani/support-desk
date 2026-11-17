"""Inputs and outputs of the policy engine.

Artefacts are plain frozen dataclasses so the reviewer, the approval gate and the evals can all
build them from database rows and run exactly the same checks.
"""

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Protocol

from app.config import Settings


@dataclass(frozen=True)
class RuleResult:
    rule: str
    passed: bool
    detail: str | None = None


class Scope(StrEnum):
    REPLY = "reply"
    EXPLAINER = "explainer"
    PULSE = "pulse"


class SourceType(StrEnum):
    CORPUS = "corpus"
    CANONICAL_EXPLAINER = "canonical_explainer"
    REFUSAL = "refusal"
    NOT_COVERED = "not_covered"
    CLARIFICATION = "clarification"


CITED_SOURCE_TYPES = frozenset(
    {SourceType.CORPUS, SourceType.CANONICAL_EXPLAINER, SourceType.REFUSAL}
)


@dataclass(frozen=True)
class Citation:
    chunk_id: str
    url: str


@dataclass(frozen=True)
class ReplyArtifact:
    text: str
    source_type: SourceType
    citation: Citation | None = None
    last_checked: date | None = None


@dataclass(frozen=True)
class ExplainerArtifact:
    bullets: tuple[str, ...]
    source_urls: tuple[str, ...]
    last_checked: date | None = None

    @property
    def text(self) -> str:
        return "\n".join(self.bullets)


@dataclass(frozen=True)
class PulseArtifact:
    text: str


@dataclass(frozen=True)
class ThemeArtifact:
    label: str
    count: int
    review_ids: tuple[str, ...] = ()
    question_ids: tuple[str, ...] = ()
    window_start: date | None = None
    window_end: date | None = None


Artifact = ReplyArtifact | ExplainerArtifact | PulseArtifact | ThemeArtifact


class CorpusIndex(Protocol):
    """Lookup into the corpus. Phase 2 provides a database-backed version."""

    def url_for_chunk(self, chunk_id: str) -> str | None: ...

    def has_url(self, url: str) -> bool: ...


@dataclass(frozen=True)
class InMemoryCorpusIndex:
    chunk_urls: dict[str, str] = field(default_factory=dict)

    def url_for_chunk(self, chunk_id: str) -> str | None:
        return self.chunk_urls.get(chunk_id)

    def has_url(self, url: str) -> bool:
        return url in self.chunk_urls.values()


@dataclass(frozen=True)
class PolicyContext:
    allowed_domains: tuple[str, ...]
    today: date
    corpus: CorpusIndex | None = None

    @classmethod
    def from_settings(
        cls, settings: Settings, *, corpus: CorpusIndex | None = None, today: date | None = None
    ) -> "PolicyContext":
        return cls(tuple(settings.source_domains), today or date.today(), corpus)
