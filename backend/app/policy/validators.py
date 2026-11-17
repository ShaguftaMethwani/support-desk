"""One function per rule in architecture section 6.2. Each returns a RuleResult.

Details name the rule that fired and, for advice and returns, the matched phrase, so the
orchestrator's single rework attempt knows exactly what to fix. PII details name only the
types found, never the values.
"""

import re

from app.guard import find_pii
from app.policy.rules import advice_rules, returns_rules
from app.policy.text import count_bullets, count_sentences, count_words, domain_allowed, find_urls
from app.policy.types import (
    CITED_SOURCE_TYPES,
    Artifact,
    ExplainerArtifact,
    PolicyContext,
    PulseArtifact,
    ReplyArtifact,
    RuleResult,
    Scope,
    SourceType,
    ThemeArtifact,
)

REPLY_MAX_SENTENCES = 3
PULSE_MAX_WORDS = 250
EXPLAINER_MAX_BULLETS = 6
EXPLAINER_SOURCE_COUNT = 2

_PERCENT = re.compile(r"\d+(?:\.\d+)?\s*(?:%|per\s*cent\b|percent\b)", re.IGNORECASE)
# Figures that make a sentence a factual claim, which must then carry a citation.
_FACT_FIGURE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|per\s*cent\b|percent\b)|₹\s*\d|\brs\.?\s*\d|"
    r"\b\d+\s*(?:days?|years?|months?|yrs?)\b",
    re.IGNORECASE,
)


def _short(text: str, limit: int = 80) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --- pii_guard --------------------------------------------------------------------------------


def pii_guard(text: str) -> RuleResult:
    found = find_pii(text, names=False)
    if found:
        return RuleResult("pii_guard", False, "found: " + ", ".join(t.value for t in found))
    return RuleResult("pii_guard", True)


# --- advice_guard -----------------------------------------------------------------------------


def advice_guard(text: str, scope: Scope) -> RuleResult:
    rules = advice_rules()
    allowed = [(m.start(), m.end()) for rule in rules.allow for m in rule.pattern.finditer(text)]
    for rule in rules.block:
        if scope.value not in rule.scope:
            continue
        for m in rule.pattern.finditer(text):
            if any(m.start() < end and start < m.end() for start, end in allowed):
                continue
            return RuleResult("advice_guard", False, f'{rule.id}: "{_short(m.group(0).strip())}"')
    return RuleResult("advice_guard", True)


# --- returns_guard ----------------------------------------------------------------------------


def _mask(text: str) -> str:
    """Blank out tax and plan-name phrases, keeping character positions unchanged."""
    return returns_rules().mask.sub(lambda m: " " * len(m.group(0)), text)


def _nearest(pattern: re.Pattern[str], text: str, start: int, end: int, window: int) -> int | None:
    lo, hi = max(0, start - window), min(len(text), end + window)
    best: int | None = None
    for m in pattern.finditer(text, lo, hi):
        distance = start - m.end() if m.end() <= start else max(0, m.start() - end)
        best = distance if best is None else min(best, distance)
    return best


def returns_guard(text: str) -> RuleResult:
    rules = returns_rules()
    masked = _mask(text)
    for claim in rules.claims:
        m = claim.pattern.search(masked)
        if m:
            return RuleResult(
                "returns_guard", False, f'{claim.id}: "{_short(text[m.start() : m.end()])}"'
            )
    for m in _PERCENT.finditer(masked):
        to_return = _nearest(rules.return_words, masked, m.start(), m.end(), rules.window_chars)
        if to_return is None:
            continue
        to_fee = _nearest(rules.fee_words, masked, m.start(), m.end(), rules.window_chars)
        if to_fee is None or to_return < to_fee:
            lo, hi = max(0, m.start() - 30), min(len(text), m.end() + 30)
            return RuleResult("returns_guard", False, f'return_figure: "{_short(text[lo:hi])}"')
    return RuleResult("returns_guard", True)


# --- citation_required ------------------------------------------------------------------------


def _bad_urls(urls: list[str], ctx: PolicyContext) -> list[str]:
    return [u for u in urls if not domain_allowed(u, list(ctx.allowed_domains))]


def citation_required(
    artifact: ReplyArtifact | ExplainerArtifact, ctx: PolicyContext
) -> RuleResult:
    name = "citation_required"
    if isinstance(artifact, ExplainerArtifact):
        bad = _bad_urls(list(artifact.source_urls) + find_urls(artifact.text), ctx)
        if bad:
            return RuleResult(name, False, f"source not on the allow-list: {bad[0]}")
        if ctx.corpus is not None:
            missing = [u for u in artifact.source_urls if not ctx.corpus.has_url(u)]
            if missing:
                return RuleResult(name, False, f"source not in the corpus: {missing[0]}")
        return RuleResult(name, True)

    bad = _bad_urls(find_urls(artifact.text), ctx)
    if bad:
        return RuleResult(name, False, f"link not on the allow-list: {bad[0]}")

    if artifact.source_type not in CITED_SOURCE_TYPES:
        figure = _FACT_FIGURE.search(artifact.text)
        if figure:
            return RuleResult(
                name,
                False,
                f'uncited factual claim in a {artifact.source_type} reply: "{figure.group(0)}"',
            )
        return RuleResult(name, True, "no citation needed")

    citation = artifact.citation
    if citation is None:
        return RuleResult(name, False, f"{artifact.source_type} reply has no citation")
    if not domain_allowed(citation.url, list(ctx.allowed_domains)):
        return RuleResult(name, False, f"citation not on the allow-list: {citation.url}")
    if ctx.corpus is not None:
        chunk_url = ctx.corpus.url_for_chunk(citation.chunk_id)
        if chunk_url is None:
            return RuleResult(name, False, f"citation chunk not in the corpus: {citation.chunk_id}")
        if chunk_url != citation.url:
            return RuleResult(name, False, "citation link does not match its corpus chunk")
    return RuleResult(name, True)


# --- length_caps ------------------------------------------------------------------------------


def length_caps(artifact: ReplyArtifact | ExplainerArtifact | PulseArtifact) -> RuleResult:
    name = "length_caps"
    if isinstance(artifact, ReplyArtifact):
        n = count_sentences(artifact.text)
        ok = 0 < n <= REPLY_MAX_SENTENCES
        detail = f"{n} sentences (max {REPLY_MAX_SENTENCES})"
    elif isinstance(artifact, PulseArtifact):
        n = count_words(artifact.text)
        ok = 0 < n <= PULSE_MAX_WORDS
        detail = f"{n} words (max {PULSE_MAX_WORDS})"
    else:
        n = len(artifact.bullets)
        empty = sum(1 for b in artifact.bullets if not b.strip())
        multiline = sum(1 for b in artifact.bullets if "\n" in b.strip() or count_bullets(b) > 1)
        ok = 0 < n <= EXPLAINER_MAX_BULLETS and not empty and not multiline
        detail = f"{n} bullets (max {EXPLAINER_MAX_BULLETS})"
        if empty or multiline:
            detail += ", each bullet must be one non-empty line"
    return RuleResult(name, ok, detail)


# --- last_checked_present ---------------------------------------------------------------------


def last_checked_present(
    artifact: ReplyArtifact | ExplainerArtifact, ctx: PolicyContext
) -> RuleResult:
    name = "last_checked_present"
    if isinstance(artifact, ReplyArtifact) and artifact.source_type not in CITED_SOURCE_TYPES:
        return RuleResult(name, True, "not required without a source")
    if artifact.last_checked is None:
        return RuleResult(name, False, "missing Last checked date")
    if artifact.last_checked > ctx.today:
        return RuleResult(
            name, False, f"Last checked date {artifact.last_checked} is in the future"
        )
    return RuleResult(name, True)


# --- explainer_sources ------------------------------------------------------------------------


def explainer_sources(artifact: ExplainerArtifact, ctx: PolicyContext) -> RuleResult:
    unique = set(artifact.source_urls)
    if len(unique) != EXPLAINER_SOURCE_COUNT or len(artifact.source_urls) != len(unique):
        return RuleResult(
            "explainer_sources",
            False,
            f"{len(unique)} unique sources (needs exactly {EXPLAINER_SOURCE_COUNT})",
        )
    bad = _bad_urls(list(unique), ctx)
    if bad:
        return RuleResult("explainer_sources", False, f"not an official source: {bad[0]}")
    return RuleResult("explainer_sources", True)


# --- theme_provenance -------------------------------------------------------------------------


def theme_provenance(theme: ThemeArtifact) -> RuleResult:
    name = "theme_provenance"
    ids = list(theme.review_ids) + list(theme.question_ids)
    if not ids:
        return RuleResult(name, False, "theme lists no review or question ids")
    if len(set(ids)) != len(ids):
        return RuleResult(name, False, "theme lists duplicate ids")
    if theme.count != len(ids):
        return RuleResult(name, False, f"count {theme.count} does not match {len(ids)} ids")
    if theme.window_start is None or theme.window_end is None:
        return RuleResult(name, False, "count has no stated window")
    if theme.window_start > theme.window_end:
        return RuleResult(name, False, "window starts after it ends")
    return RuleResult(name, True)


__all__ = [
    "Artifact",
    "SourceType",
    "advice_guard",
    "citation_required",
    "explainer_sources",
    "last_checked_present",
    "length_caps",
    "pii_guard",
    "returns_guard",
    "theme_provenance",
]
