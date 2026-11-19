"""The weekly loop's model calls: theme labels, quote choice, fee explainer, pulse.

The model writes words; code owns every fact:
- Counts, windows and fee figures are written by code, never by the model.
- Quotes: the model picks review ids; code copies the text and checks it is a substring of the
  stored review.
- Explainer: every bullet cites a passage, its numbers must appear in that passage, and code
  attaches the two source links and the Last checked date.
Each artefact runs through the policy engine. A failure gets one regeneration that names the
failed rule; a second failure raises PolicyFailure and the run is marked failed.
"""

import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Protocol

from pydantic import BaseModel, Field

from app.agents.facts import unsupported_numbers
from app.corpus.retrieve import RetrievedChunk, SearchTrace
from app.llm import LLMOutputError, LLMResult
from app.policy import (
    ExplainerArtifact,
    InMemoryCorpusIndex,
    PolicyContext,
    PulseArtifact,
    RuleResult,
    Scope,
    failures,
    run_policy,
)
from app.policy.text import count_words, split_sentences
from app.policy.validators import (
    EXPLAINER_MAX_BULLETS,
    PULSE_MAX_WORDS,
    advice_guard,
    pii_guard,
    returns_guard,
)
from app.weekly.fees import FeeDetection, FeeType

# Document types the fee explainer may draw on. Factsheets are left out: their chunks are
# mostly performance tables, which an explainer must not quote.
FEE_DOC_TYPES = ("fees", "kim", "sid", "sai", "faq", "regulation", "education")
EXPLAINER_PASSAGES = 6
CANDIDATES_PER_QUERY = 10
PER_SOURCE = 3
EXAMPLES_PER_THEME = 8
QUOTE_CANDIDATES = 6
QUOTE_MAX_CHARS = 220
QUOTE_MIN_CHARS = 30
ACTION_COUNT = 3
_REDACTION_TOKEN = re.compile(r"\[[A-Z_]+_REDACTED\]")
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


class Completer(Protocol):
    async def complete(
        self,
        task: str,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: type[BaseModel] | None = None,
        json_mode: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> LLMResult: ...


class Retriever(Protocol):
    async def search(
        self,
        query: str,
        *,
        scheme: str | None = None,
        include_platform: bool = False,
        k: int = 4,
        doc_types: Sequence[str] | None = None,
    ) -> tuple[list[RetrievedChunk], SearchTrace]: ...


class PolicyFailure(Exception):
    def __init__(self, artefact: str, results: list[RuleResult]) -> None:
        self.artefact = artefact
        self.results = results
        detail = "; ".join(f"{r.rule}: {r.detail}" for r in results)
        super().__init__(f"{artefact} failed validation twice ({detail})")


def describe_failures(results: list[RuleResult]) -> str:
    return "\n".join(f"- {r.rule}: {r.detail}" for r in results)


@dataclass(frozen=True)
class Validated[T]:
    value: T
    attempts: int
    first_failures: list[RuleResult]  # why the first attempt was rejected, if it was

    def log(self) -> dict[str, object]:
        out: dict[str, object] = {"attempts": self.attempts}
        if self.first_failures:
            out["first_attempt_failed"] = [
                {"rule": r.rule, "detail": r.detail} for r in self.first_failures
            ]
        return out


async def generate_validated[T](
    artefact: str,
    generate: Callable[[str | None], Awaitable[T]],
    check: Callable[[T], list[RuleResult]],
) -> Validated[T]:
    """Generate, validate, and regenerate once with the named failures. Raises PolicyFailure
    if the second attempt also fails."""
    first = await generate(None)
    first_failed = failures(check(first))
    if not first_failed:
        return Validated(first, 1, [])
    second = await generate(describe_failures(first_failed))
    failed = failures(check(second))
    if failed:
        raise PolicyFailure(artefact, failed)
    return Validated(second, 2, first_failed)


def _parsed[T](result: LLMResult, schema: type[T]) -> T:
    if not isinstance(result.parsed, schema):
        raise LLMOutputError(f"{result.task}: no parsed output")
    return result.parsed


# --- Theme labels -------------------------------------------------------------------------

THEME_PROMPT = """You name themes in app store reviews of an investing app, for its product team.
You get example reviews from one group of similar reviews.

Reply with JSON only: {"label": "...", "description": "..."}
- label: 2 to 5 words naming what these users talk about, e.g. "Payment and UPI failures".
- description: one sentence, at most 20 words, saying what users report.
- No names, no personal details, no numbers, no advice."""


QUESTION_THEME_PROMPT = """You name themes in customer questions to an investing app's support
desk, for its product team. You get example questions from one group of similar questions.

Reply with JSON only: {"label": "...", "description": "..."}
- label: 2 to 5 words naming what these customers ask about, e.g. "Exit load on redemption".
- description: one sentence, at most 20 words, saying what customers ask.
- No names, no personal details, no numbers, no advice."""


class ThemeLabel(BaseModel):
    label: str = Field(min_length=2, max_length=80)
    description: str = Field(min_length=2, max_length=300)


async def label_theme(
    llm: Completer,
    examples: list[str],
    feedback: str | None = None,
    *,
    questions: bool = False,
) -> ThemeLabel:
    """Name a theme from its examples: app store reviews, or support-queue questions."""
    shown = "\n".join(f"- {e[:300]}" for e in examples[:EXAMPLES_PER_THEME])
    user = f"Example {'questions' if questions else 'reviews'}:\n{shown}"
    if feedback:
        user += f"\n\nYour previous label was rejected:\n{feedback}\nFix it."
    prompt = QUESTION_THEME_PROMPT if questions else THEME_PROMPT
    result = await llm.complete(
        "theme_label",
        [{"role": "system", "content": prompt}, {"role": "user", "content": user}],
        schema=ThemeLabel,
        max_tokens=300,
    )
    parsed = _parsed(result, ThemeLabel)
    return ThemeLabel(label=" ".join(parsed.label.split()).strip(" .\"'"),
                      description=" ".join(parsed.description.split()))  # fmt: skip


# --- Quotes -------------------------------------------------------------------------------


@dataclass(frozen=True)
class QuoteCandidate:
    review_id: str
    theme_index: int  # position in the top themes list, 0-based
    text: str  # the quote: a prefix of the stored review text


@dataclass(frozen=True)
class Quote:
    review_id: str
    theme_index: int
    text: str


def excerpt(text: str) -> str | None:
    """A quotable prefix of a review: the whole text if short, else its leading sentences up to
    QUOTE_MAX_CHARS. None when there is no clean sentence break."""
    text = text.strip()
    if len(text) <= QUOTE_MAX_CHARS:
        return text if len(text) >= QUOTE_MIN_CHARS else None
    ends = [m.end() for m in _SENTENCE_END.finditer(text, 0, QUOTE_MAX_CHARS + 1)]
    ends = [e for e in ends if e >= QUOTE_MIN_CHARS]
    return text[: ends[-1]] if ends else None


def quotable(text: str) -> bool:
    """Code checks a quote must pass before the model may choose it."""
    if _REDACTION_TOKEN.search(text):
        return False
    return all(
        r.passed for r in (pii_guard(text), advice_guard(text, Scope.PULSE), returns_guard(text))
    )


def quote_candidates(themes: list[list[tuple[str, str]]]) -> list[QuoteCandidate]:
    """themes: for each top theme, (review_id, text) pairs, closest to the centre first."""
    out: list[QuoteCandidate] = []
    for t, reviews in enumerate(themes):
        n = 0
        for review_id, text in reviews:
            quote = excerpt(text)
            if quote is None or not quotable(quote):
                continue
            out.append(QuoteCandidate(review_id, t, quote))
            n += 1
            if n == QUOTE_CANDIDATES:
                break
    return out


QUOTE_PROMPT = """You choose customer quotes for a weekly product report.
You get candidate quotes, each with an id, grouped by theme.

Pick 3 quotes: one per theme where possible, each clearly showing its theme in the user's own
words. Prefer specific, readable quotes over vague praise.

Reply with JSON only: {"picks": ["<id>", "<id>", "<id>"]}. Use ids exactly as given."""


class QuotePicks(BaseModel):
    picks: list[str] = Field(default_factory=list)


async def pick_quotes(
    llm: Completer, theme_labels: list[str], candidates: list[QuoteCandidate], n: int = 3
) -> tuple[list[Quote], str]:
    """Returns the quotes and how they were picked ("model" or "fallback")."""
    if not candidates:
        return [], "none"
    ids = {f"Q{i + 1}": c for i, c in enumerate(candidates)}
    blocks = []
    for t, label in enumerate(theme_labels):
        lines = [f'  {qid}: "{c.text}"' for qid, c in ids.items() if c.theme_index == t]
        if lines:
            blocks.append(f"Theme: {label}\n" + "\n".join(lines))
    try:
        result = await llm.complete(
            "quote_pick",
            [
                {"role": "system", "content": QUOTE_PROMPT},
                {"role": "user", "content": "\n\n".join(blocks)},
            ],  # fmt: skip
            schema=QuotePicks,
            max_tokens=300,
        )
        picked = [ids[p] for p in dict.fromkeys(_parsed(result, QuotePicks).picks) if p in ids]
    except LLMOutputError:
        picked = []
    how = "model"
    if len(picked) < min(n, len(candidates)):
        # Fill from the candidates closest to each theme's centre, one theme at a time.
        how = "fallback" if not picked else "model+fallback"
        for c in sorted(candidates, key=lambda c: c.theme_index):
            if len(picked) >= n:
                break
            if c not in picked and all(p.theme_index != c.theme_index for p in picked):
                picked.append(c)
        for c in candidates:
            if len(picked) >= n:
                break
            if c not in picked:
                picked.append(c)
    return [Quote(c.review_id, c.theme_index, c.text) for c in picked[:n]], how


def verify_quotes(quotes: list[Quote], stored: Mapping[str, str]) -> list[str]:
    """Problems found; empty when every quote is a verbatim substring of its stored review."""
    problems = []
    for q in quotes:
        source = stored.get(q.review_id)
        if source is None:
            problems.append(f"{q.review_id}: review not found")
        elif q.text not in source:
            problems.append(f"{q.review_id}: quote is not a substring of the review")
    return problems


# --- Fee explainer ------------------------------------------------------------------------


@dataclass(frozen=True)
class ExplainerDraft:
    bullets: tuple[str, ...]
    source_urls: tuple[str, ...]
    source_keys: tuple[str, ...]
    last_checked: date | None
    chunk_ids: tuple[str, ...]
    dropped_bullets: int  # bullets removed by code (bad passage, unsupported number, 3rd source)

    def artifact(self) -> ExplainerArtifact:
        return ExplainerArtifact(self.bullets, self.source_urls, self.last_checked)


async def explainer_passages(
    retriever: Retriever, fee: FeeType, *, min_similarity: float
) -> list[RetrievedChunk]:
    """The best passages across the fee's queries, at most PER_SOURCE from any one source, so
    the explainer can cite two sources even when one document dominates the search."""
    best: dict[str, RetrievedChunk] = {}
    for query in fee.queries:
        chunks, _ = await retriever.search(query, k=CANDIDATES_PER_QUERY, doc_types=FEE_DOC_TYPES)
        for c in chunks:
            if c.similarity >= min_similarity and (
                c.chunk_id not in best or c.score > best[c.chunk_id].score
            ):
                best[c.chunk_id] = c
    chosen: list[RetrievedChunk] = []
    per_source: dict[str, int] = {}
    for c in sorted(best.values(), key=lambda c: (-c.score, c.chunk_id)):
        if per_source.get(c.url, 0) < PER_SOURCE:
            chosen.append(c)
            per_source[c.url] = per_source.get(c.url, 0) + 1
        if len(chosen) == EXPLAINER_PASSAGES:
            break
    return chosen


EXPLAINER_PROMPT = """You write a short fee explainer that support agents share with customers of an
Indian mutual fund investing app.

Rules:
- Use ONLY the numbered passages. Never use outside knowledge.
- At most 6 bullets. Each bullet is one plain sentence a customer can understand.
- Every bullet cites the one passage it comes from. Copy any figure exactly as written there.
- Use passages from exactly TWO different sources (see each passage's Source line).
- Facts only: no advice, no recommendations, no returns or performance figures.

Reply with JSON only: {"bullets": [{"text": "...", "passage": 1}]}"""


class _Bullet(BaseModel):
    text: str
    passage: int


class _ExplainerOut(BaseModel):
    bullets: list[_Bullet] = Field(default_factory=list)


async def write_explainer(
    llm: Completer, fee: FeeType, passages: list[RetrievedChunk], feedback: str | None = None
) -> ExplainerDraft:
    shown = "\n\n".join(
        f"[{i}] Source: {c.title or c.source_key} ({c.publisher})\n{c.text}"
        for i, c in enumerate(passages, start=1)
    )
    user = f"Topic: {fee.label}\n\nPassages:\n{shown}"
    if feedback:
        user += f"\n\nYour previous explainer was rejected:\n{feedback}\nFix it."
    result = await llm.complete(
        "fee_explainer",
        [{"role": "system", "content": EXPLAINER_PROMPT}, {"role": "user", "content": user}],
        schema=_ExplainerOut,
        max_tokens=900,
    )
    out = _parsed(result, _ExplainerOut)
    kept: list[tuple[str, RetrievedChunk]] = []
    dropped = 0
    for b in out.bullets:
        text = " ".join(b.text.split()).lstrip("-•* ").strip()
        if not text or not 1 <= b.passage <= len(passages):
            dropped += 1
            continue
        chunk = passages[b.passage - 1]
        if unsupported_numbers(text, chunk.text):
            dropped += 1
            continue
        kept.append((text, chunk))
    # Exactly two sources: keep the two cited most often (first cited wins a tie).
    order: list[str] = []
    counts: dict[str, int] = {}
    for _, c in kept:
        if c.url not in counts:
            order.append(c.url)
        counts[c.url] = counts.get(c.url, 0) + 1
    top_two = sorted(order, key=lambda u: (-counts[u], order.index(u)))[:2]
    final = [(t, c) for t, c in kept if c.url in top_two]
    dropped += len(kept) - len(final)
    final = final[:EXPLAINER_MAX_BULLETS]
    urls = tuple(u for u in order if u in top_two)
    by_url = {c.url: c for _, c in final}
    dates = []
    for u in urls:
        fetched = by_url[u].fetched_at if u in by_url else None
        if fetched is not None:
            dates.append(fetched.date())
    return ExplainerDraft(
        bullets=tuple(t for t, _ in final),
        source_urls=urls,
        source_keys=tuple(by_url[u].source_key for u in urls if u in by_url),
        last_checked=min(dates) if dates else None,
        chunk_ids=tuple(c.chunk_id for _, c in final),
        dropped_bullets=dropped,
    )


def explainer_context(
    passages: list[RetrievedChunk], allowed_domains: tuple[str, ...], today: date
) -> PolicyContext:
    index = InMemoryCorpusIndex({c.chunk_id: c.url for c in passages})
    return PolicyContext(allowed_domains, today, index)


def check_explainer(draft: ExplainerDraft, ctx: PolicyContext) -> list[RuleResult]:
    return run_policy(draft.artifact(), ctx)


# --- Weekly pulse -------------------------------------------------------------------------


@dataclass(frozen=True)
class PulseTheme:
    label: str
    description: str
    count: int


@dataclass(frozen=True)
class SupportTheme:
    """A surfaced support-queue question cluster (architecture section 9.1)."""

    label: str
    count: int
    window_start: date
    window_end: date

    @property
    def line(self) -> str:
        """Written by code into the pulse; the pulse check looks for exactly this."""
        return (f"{self.label} ({self.count} questions, "
                f"{self.window_start:%d %b} to {self.window_end:%d %b})")  # fmt: skip


@dataclass(frozen=True)
class PulseInput:
    window_start: date
    window_end: date
    reviews_analysed: int
    reviews_themed: int  # long enough to cluster
    themes: list[PulseTheme]  # the top 3, highest count first
    quotes: list[tuple[str, str]]  # (quote text, theme label)
    fees: FeeDetection
    explainer_attached: bool
    support_themes: list[SupportTheme] = field(default_factory=list)


PULSE_PROMPT = """You write the weekly product pulse for an investing app's product team, from app
store review themes.

Reply with JSON only:
{"summary": "...", "themes": ["...", "...", "..."], "actions": ["...", "...", "..."]}
- summary: one or two sentences on what stood out this week.
- themes: one short sentence per theme, in the order given, saying what users report.
- actions: exactly 3 concrete product action ideas, each one short sentence.
- Do not write numbers or counts; code adds them.
- No names, no personal details, no investment advice, no return or performance figures.
- Keep the whole reply under {budget} words."""


class _PulseOut(BaseModel):
    summary: str
    themes: list[str] = Field(default_factory=list)
    # Exactly 3: a wrong count fails schema validation, which gets the client's repair retry.
    actions: list[str] = Field(min_length=ACTION_COUNT, max_length=ACTION_COUNT)


def _platform_charges(fees: FeeDetection) -> str | None:
    parts = [f"{c.label.lower()} ({c.count})" for c in fees.counts if not c.mutual_fund]
    return ", ".join(parts) if parts else None


def fee_lines(inp: PulseInput) -> list[str]:
    fees = inp.fees
    if fees.fee_type is None:
        lines = ["Fee confusion: none detected this week."]
    else:
        n = len(fees.evidence_review_ids)
        line = f"Fee confusion: {fees.label} ({n} reviews)."
        if inp.explainer_attached:
            line += " A fee explainer for support is attached."
        lines = [line]
    platform = _platform_charges(fees)
    if platform:
        lines.append(f"Charge complaints outside mutual funds (reviews): {platform}.")
    return lines


# When the pulse is over the word cap with many surfaced clusters, it names the largest few and
# counts the rest; the doc entry still lists every cluster with its queue item ids.
SUPPORT_LINES_SHOWN = 3


def _more_line(hidden: int) -> str:
    return f"- and {hidden} more, listed in the doc entry"


def support_lines(support: Sequence[SupportTheme], limit: int | None = None) -> list[str]:
    if limit is None or len(support) <= limit:
        return [f"- {t.line}" for t in support]
    largest = sorted(support, key=lambda t: -t.count)[:limit]
    return [f"- {t.line}" for t in largest] + [_more_line(len(support) - limit)]


def compose_pulse(inp: PulseInput, out: _PulseOut, support_limit: int | None = None) -> str:
    lines = [
        f"Weekly Product Pulse: {inp.window_start:%d %b %Y} to {inp.window_end:%d %b %Y}",
        f"{inp.reviews_analysed} reviews in the window, {inp.reviews_themed} long enough to group"
        " into themes.",
        "",
    ]
    summary = " ".join(out.summary.split())
    if summary:
        lines += [summary, ""]
    lines += ["Top themes:"]
    for i, theme in enumerate(inp.themes):
        said = out.themes[i] if i < len(out.themes) else theme.description
        lines.append(f"- {theme.label} ({theme.count} reviews): {' '.join(said.split())}")
    if inp.support_themes:
        lines += ["", "From the support queue (similar customer questions):"]
        lines += support_lines(inp.support_themes, support_limit)
    if inp.quotes:
        lines += ["", "What users said:"]
        lines += [f'- "{text}" ({label})' for text, label in inp.quotes]
    lines += ["", *fee_lines(inp), "", "Action ideas:"]
    lines += [f"{i}. {' '.join(a.split())}" for i, a in enumerate(out.actions[:ACTION_COUNT], 1)]
    return "\n".join(lines)


def shorten_quote(text: str, limit: int) -> str:
    """The quote's opening words, cut at a word boundary and marked with an ellipsis. Still a
    verbatim excerpt: the text before the ellipsis is a prefix of the review."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-")
    return f"{cut}…"


PULSE_QUOTE_LIMITS = (120, 80)
PULSE_QUOTE_LIMIT_LAST = 50  # the shortest quotes, tried before naming only 3 clusters


def fit_pulse(inp: PulseInput, out: _PulseOut) -> str:
    """The composed pulse, shortened by code when it is over the word cap. In order: the
    model's summary goes to its first sentence, then is left out; each quote is cut to its
    opening words, more and more; only then, past 3 surfaced support-queue clusters, are the 3
    largest named and the rest counted. Counts, themes, the fee lines and the action ideas are
    never cut. If it is still too long, the length check fails it as before."""
    text = compose_pulse(inp, out)
    if count_words(text) <= PULSE_MAX_WORDS:
        return text
    sentences = split_sentences(" ".join(out.summary.split()))
    first = sentences[0] if sentences else ""
    # (summary, quote length limit, support-queue clusters named)
    steps: list[tuple[str, int | None, int | None]] = [
        (first, None, None),
        ("", None, None),
        *(("", limit, None) for limit in PULSE_QUOTE_LIMITS),
        # Shorter quotes before fewer clusters: naming every surfaced cluster is how the
        # support queue reaches the product team (the loop connection).
        ("", PULSE_QUOTE_LIMIT_LAST, None),
        ("", PULSE_QUOTE_LIMIT_LAST, SUPPORT_LINES_SHOWN),
    ]
    for summary, limit, shown in steps:
        quotes = (
            inp.quotes
            if limit is None
            else [(shorten_quote(q, limit), label) for q, label in inp.quotes]
        )
        text = compose_pulse(
            replace(inp, quotes=quotes), out.model_copy(update={"summary": summary}), shown
        )
        if count_words(text) <= PULSE_MAX_WORDS:
            break
    return text


def model_word_budget(inp: PulseInput) -> int:
    """Words left for the model after the parts code writes."""
    blank = _PulseOut.model_construct(summary="", themes=["" for _ in inp.themes], actions=[])
    fixed = compose_pulse(inp, blank)
    return max(60, PULSE_MAX_WORDS - count_words(fixed) - 25)  # slack: models overshoot


async def write_pulse(llm: Completer, inp: PulseInput, feedback: str | None = None) -> str:
    budget = model_word_budget(inp)
    themes = "\n".join(
        f"{i}. {t.label}: {t.description}" for i, t in enumerate(inp.themes, start=1)
    )
    fee = " ".join(fee_lines(inp))
    user = f"Themes, most mentioned first:\n{themes}\n\nFees: {fee}"
    if inp.support_themes:
        asked = "\n".join(f"- {t.label}" for t in inp.support_themes)
        user += f"\n\nCustomers keep asking support about:\n{asked}"
    if feedback:
        user += f"\n\nYour previous pulse was rejected:\n{feedback}\nFix it."
    result = await llm.complete(
        "weekly_pulse",
        [
            {"role": "system", "content": PULSE_PROMPT.replace("{budget}", str(budget))},
            {"role": "user", "content": user},
        ],  # fmt: skip
        schema=_PulseOut,
        max_tokens=900,
    )
    return fit_pulse(inp, _parsed(result, _PulseOut))


def support_themes_reported(text: str, support: Sequence[SupportTheme]) -> RuleResult:
    """No surfaced support-queue cluster may be missing from the pulse (Phase 6). Each one is
    named with its count and window, or, when the pulse names only the largest few to stay
    under the word cap, the rest are counted on the "and N more" line."""
    missing = [t for t in support if t.line not in text]
    if not missing:
        return RuleResult("support_queue_reported", True, f"{len(support)} surfaced clusters")
    shown = SUPPORT_LINES_SHOWN
    largest = sorted(support, key=lambda t: -t.count)[:shown]
    if (
        len(support) > shown
        and all(t.line in text for t in largest)
        and _more_line(len(support) - shown) in text
    ):
        detail = f"{shown} of {len(support)} surfaced clusters named, the rest counted"
        return RuleResult("support_queue_reported", True, detail)
    detail = f"missing from the pulse: {missing[0].label}"
    return RuleResult("support_queue_reported", False, detail)


def check_pulse(
    text: str, ctx: PolicyContext, support: Sequence[SupportTheme] = ()
) -> list[RuleResult]:
    return [*run_policy(PulseArtifact(text), ctx), support_themes_reported(text, support)]
