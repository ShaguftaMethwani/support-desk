"""Facts Agent (architecture section 7.4).

Answers from the corpus only, with exactly one citation, or says "not covered" and names
what it searched. It never falls back on general knowledge. Four checks lead to not_covered:
  1. low_similarity: no retrieved chunk is close enough to the question (no model call)
  2. model_not_covered: the model says the passages don't answer the question
  3. bad_passage: the model cites a passage number that wasn't given
  4. number_mismatch: the answer contains a number that isn't in the cited passage
Model errors (rate limits exhausted, invalid JSON) are raised, not hidden as "not covered":
the caller marks the item temporarily unavailable.
"""

import re
from datetime import date
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

from app.corpus.retrieve import RetrievedChunk, SearchTrace
from app.llm import LLMClient

TOP_K = 4

SYSTEM_PROMPT = """You extract facts about Indian mutual fund schemes from official documents.

Rules:
- Use ONLY the numbered passages. Never use outside knowledge.
- If the passages do not clearly answer the question, reply {"not_covered": true}.
- Otherwise answer in at most 2 short sentences. Copy figures exactly as written.
- Facts only. No advice, recommendations, opinions, or return or performance figures.
- Cite exactly one passage: the number of the passage that contains the answer.

Reply with JSON only: {"not_covered": false, "answer": "...", "passage": 2}"""


class Retriever(Protocol):
    async def search(
        self, query: str, *, scheme: str | None = None, include_platform: bool = False, k: int = 4
    ) -> tuple[list[RetrievedChunk], SearchTrace]: ...


class SourceCitation(BaseModel):
    chunk_id: str
    source_key: str
    url: str
    title: str | None
    page: int | None
    last_checked: date | None


class FactsResult(BaseModel):
    status: Literal["answered", "not_covered"]
    answer: str | None = None
    citation: SourceCitation | None = None
    reason: str | None = None  # why not covered
    searched: list[dict[str, Any]] = Field(default_factory=list)
    searched_summary: str = ""


class _ModelAnswer(BaseModel):
    not_covered: bool = False
    answer: str | None = None
    passage: int | None = None


_NUMBER = re.compile(r"\d+(?:,\d{2,3})*(?:\.\d+)?")


def numbers_in(text: str) -> set[float]:
    return {float(m.group(0).replace(",", "")) for m in _NUMBER.finditer(text)}


def unsupported_numbers(answer: str, passage: str) -> set[float]:
    """Numbers stated in the answer that the cited passage doesn't contain."""
    return numbers_in(answer) - numbers_in(passage)


def _passages(chunks: list[RetrievedChunk]) -> str:
    parts = []
    for i, c in enumerate(chunks, start=1):
        label = c.title or c.source_key
        meta = f"Source: {label}" + (f" | Section: {c.section}" if c.section else "")
        parts.append(f"[{i}] {meta}\n{c.text}")
    return "\n\n".join(parts)


class FactsAgent:
    def __init__(self, retriever: Retriever, llm: LLMClient, *, min_similarity: float) -> None:
        self._retriever = retriever
        self._llm = llm
        self._min_similarity = min_similarity

    async def run(
        self, question: str, *, scheme: str | None = None, include_platform: bool = False
    ) -> FactsResult:
        chunks, trace = await self._retriever.search(
            question, scheme=scheme, include_platform=include_platform, k=TOP_K
        )

        def not_covered(reason: str) -> FactsResult:
            return FactsResult(
                status="not_covered",
                reason=reason,
                searched=trace.top,
                searched_summary=trace.summary(),
            )

        if not chunks or max(c.similarity for c in chunks) < self._min_similarity:
            return not_covered("low_similarity")

        result = await self._llm.complete(
            "facts",
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Question: {question}\n\nPassages:\n{_passages(chunks)}",
                },
            ],
            schema=_ModelAnswer,
            max_tokens=400,
        )
        parsed = result.parsed
        assert isinstance(parsed, _ModelAnswer)
        if parsed.not_covered or not (parsed.answer or "").strip():
            return not_covered("model_not_covered")
        if parsed.passage is None or not 1 <= parsed.passage <= len(chunks):
            return not_covered("bad_passage")
        cited = chunks[parsed.passage - 1]
        answer = parsed.answer.strip() if parsed.answer else ""
        if unsupported_numbers(answer, cited.text):
            return not_covered("number_mismatch")
        return FactsResult(
            status="answered",
            answer=answer,
            citation=SourceCitation(
                chunk_id=cited.chunk_id,
                source_key=cited.source_key,
                url=cited.url,
                title=cited.title,
                page=cited.page,
                last_checked=cited.fetched_at.date() if cited.fetched_at else None,
            ),
            searched=trace.top,
            searched_summary=trace.summary(),
        )
