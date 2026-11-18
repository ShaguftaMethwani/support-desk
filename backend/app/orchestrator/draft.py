"""Orchestrator drafting (architecture section 7.5).

Code decides the route and writes every template; the large model only rewords a Facts
answer into a reply, and never writes URLs. Code attaches the citation and Last checked lines.

- corpus answer: the model writes at most 3 sentences from the Facts answer alone. Any number
  not in the Facts answer or its passage, or any URL, makes code fall back to the Facts
  answer verbatim.
- advice and returns: refusal templates, citing an AMFI or SEBI education page (advice) or
  the scheme's factsheet (returns), found by retrieval in the corpus.
- not covered: a template that says so and that the question was passed on. No figures.
- unclear: one clarifying question (template).
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from typing import Protocol

from pydantic import BaseModel

from app.agents.facts import FactsResult, SourceCitation, numbers_in
from app.corpus.retrieve import RetrievedChunk, SearchTrace
from app.corpus.schemes import BY_SLUG, SCHEMES
from app.llm import LLMError, LLMResult
from app.orchestrator.routing import RoutingNote
from app.policy import Citation, ReplyArtifact, SourceType
from app.policy.text import find_urls

ADVICE_DOC_TYPES = ("education", "regulation", "faq")  # AMFI and SEBI investor material
RETURNS_DOC_TYPES = ("factsheet",)


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


@dataclass(frozen=True)
class Draft:
    body: str  # the reply sentences, without the Source and Last checked lines
    source_type: SourceType
    citation: SourceCitation | None
    routing: RoutingNote
    model_written: bool  # the model wrote the body (only these get rework and the judge)
    fallback_used: bool = False  # the model's wording was rejected by code checks
    canonical_id: uuid.UUID | None = None  # the canonical explainer the body came from

    @property
    def last_checked(self) -> date | None:
        return self.citation.last_checked if self.citation else None

    @property
    def text(self) -> str:
        lines = [self.body.strip()]
        if self.citation is not None:
            lines.append(f"Source: {self.citation.url}")
        if self.last_checked is not None:
            lines.append(f"Last checked: {self.last_checked.isoformat()}")
        return "\n".join(lines)

    def artifact(self) -> ReplyArtifact:
        citation = (Citation(self.citation.chunk_id, self.citation.url)
                    if self.citation else None)  # fmt: skip
        return ReplyArtifact(self.text, self.source_type, citation, self.last_checked)

    def with_body(self, body: str) -> "Draft":
        return replace(self, body=body)


def citation_from(chunk: RetrievedChunk) -> SourceCitation:
    return SourceCitation(
        chunk_id=chunk.chunk_id, source_key=chunk.source_key, url=chunk.url, title=chunk.title,
        page=chunk.page, last_checked=chunk.fetched_at.date() if chunk.fetched_at else None,
    )  # fmt: skip


# --- templates ------------------------------------------------------------------------------

ADVICE_REFUSAL = (
    "I can only share facts from official scheme documents, not investment advice or "
    "recommendations. AMFI and SEBI publish investor education on how schemes differ by "
    "category and risk, linked below."
)
RETURNS_REFUSAL = (
    "I can't share or compare returns or performance figures. The scheme's official factsheet, "
    "linked below, has its latest published figures."
)
NOT_COVERED = (
    "I couldn't find this in the official documents I can cite, so I've passed your question "
    "to our team to answer."
)
# The customer-facing text never names an internal team: the routing note does that.
COMPLAINT_NOT_COVERED = (
    "Sorry about the trouble with this. I've passed it to our team, who will look into it and "
    "get back to you."
)


def clarification(scheme: str | None) -> str:
    if scheme is None:
        names = [s.name for s in SCHEMES]
        return f"Which scheme do you mean: {', '.join(names[:-1])} or {names[-1]}?"
    return (f"What would you like to know about {BY_SLUG[scheme].name}, for example its exit "
            "load, expense ratio, minimum SIP or lock-in?")  # fmt: skip


async def refusal_source(
    retriever: Retriever, question: str, intent: str, scheme: str | None
) -> SourceCitation | None:
    if intent == "returns":
        chunks, _ = await retriever.search(
            question, scheme=scheme, k=1, doc_types=RETURNS_DOC_TYPES
        )
    else:
        chunks, _ = await retriever.search(question, k=1, doc_types=ADVICE_DOC_TYPES)
    return citation_from(chunks[0]) if chunks else None


# --- model drafting -------------------------------------------------------------------------

DRAFT_PROMPT = """You write a short reply that a support agent will send to a customer of an
Indian investing app.

Rules:
- Use ONLY the facts given. Never add facts, figures or outside knowledge.
- At most 3 short sentences. Copy figures exactly as given.
- No advice, recommendations, opinions, or return or performance figures.
- No URLs or links: they are added separately.
- For a complaint, start with one short sentence acknowledging the problem.

Reply with JSON only: {"reply": "..."}"""


class _DraftOut(BaseModel):
    reply: str


async def write_answer(
    llm: Completer,
    *,
    question: str,
    facts: FactsResult,
    passage_text: str,
    intent: str,
    feedback: str | None = None,
    rework: str | None = None,
) -> tuple[str, bool]:
    """Reply body from a Facts answer. Returns (body, fallback_used)."""
    assert facts.answer is not None
    user = f"Question: {question}\nIntent: {intent}\nFacts: {facts.answer}"
    if feedback:
        user += f"\n\nThe support agent asked for this change: {feedback}"
    if rework:
        user += f"\n\nYour previous reply was rejected by the reviewer:\n{rework}\nFix it."
    try:
        out = await llm.complete(
            "orchestrate",
            [{"role": "system", "content": DRAFT_PROMPT}, {"role": "user", "content": user}],
            schema=_DraftOut,
            max_tokens=500,
        )
    except LLMError:
        return facts.answer, True
    parsed = out.parsed
    body = " ".join(parsed.reply.split()) if isinstance(parsed, _DraftOut) else ""
    allowed = numbers_in(facts.answer) | numbers_in(passage_text)
    if not body or find_urls(body) or not numbers_in(body) <= allowed:
        return facts.answer, True
    return body, False
