"""run_policy(): the one entry point shared by the reviewer, the approval gate and the evals."""

from app.policy.types import (
    Artifact,
    ExplainerArtifact,
    PolicyContext,
    PulseArtifact,
    ReplyArtifact,
    RuleResult,
    Scope,
    ThemeArtifact,
)
from app.policy.validators import (
    advice_guard,
    citation_required,
    explainer_sources,
    last_checked_present,
    length_caps,
    pii_guard,
    returns_guard,
    theme_provenance,
)


def run_policy(artifact: Artifact, ctx: PolicyContext) -> list[RuleResult]:
    if isinstance(artifact, ReplyArtifact):
        return [
            pii_guard(artifact.text),
            advice_guard(artifact.text, Scope.REPLY),
            returns_guard(artifact.text),
            citation_required(artifact, ctx),
            length_caps(artifact),
            last_checked_present(artifact, ctx),
        ]
    if isinstance(artifact, ExplainerArtifact):
        return [
            pii_guard(artifact.text),
            advice_guard(artifact.text, Scope.EXPLAINER),
            returns_guard(artifact.text),
            citation_required(artifact, ctx),
            length_caps(artifact),
            last_checked_present(artifact, ctx),
            explainer_sources(artifact, ctx),
        ]
    if isinstance(artifact, PulseArtifact):
        return [
            pii_guard(artifact.text),
            advice_guard(artifact.text, Scope.PULSE),
            returns_guard(artifact.text),
            length_caps(artifact),
        ]
    if isinstance(artifact, ThemeArtifact):
        return [pii_guard(artifact.label), theme_provenance(artifact)]
    raise TypeError(f"Unknown artefact type: {type(artifact).__name__}")


def failures(results: list[RuleResult]) -> list[RuleResult]:
    return [r for r in results if not r.passed]


def passed(results: list[RuleResult]) -> bool:
    return not failures(results)
