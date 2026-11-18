"""Hybrid retrieval: vector similarity first, with a full-text boost.

Candidates are the union of the top pgvector matches and the top Postgres full-text matches.
Each candidate is scored by its cosine similarity to the question, plus a small boost when it
is also a strong keyword match. Text search therefore adds candidates the vector search
missed (exact numbers, names, section titles) and breaks near-ties, without letting broad
keyword matches outrank the semantically right chunk.

Plain reciprocal rank fusion was tried first and scored 83% on the retrieval test set: the
OR-style keyword query matches common words like "fund" and "SIP" in many chunks and pulled
the right chunk down. Vector-first scored 100% for every boost size tested (0 to 0.08).

Filters:
- scheme: when known, only chunks for that scheme or general chunks.
- platform pages (INDMoney help) are excluded unless the question is about the app itself.
- doc_types: optionally, only these document types.
Every search returns a SearchTrace, so a "not covered" answer can say what was searched.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.corpus.embed import Embedder
from app.corpus.schemes import GENERAL

CANDIDATES = 20
TEXT_BOOST_TOP3 = 0.03
TEXT_BOOST_TOP10 = 0.015


@dataclass(frozen=True)
class RetrievedChunk:
    chunk_id: str
    source_key: str
    url: str
    title: str | None
    publisher: str
    doc_type: str
    scheme: str | None
    section: str | None
    page: int | None
    text: str
    fetched_at: datetime | None
    similarity: float  # cosine similarity between question and chunk
    text_rank: int | None  # position in the keyword results, None if not a keyword match
    score: float  # similarity plus keyword boost; results are sorted by this


@dataclass(frozen=True)
class SearchTrace:
    query: str
    scheme_filter: str | None
    include_platform: bool
    top: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> str:
        sources = sorted({t["source"] for t in self.top})
        scope = f"scheme {self.scheme_filter}" if self.scheme_filter else "all schemes"
        found = ", ".join(sources) or "none"
        return f'searched "{self.query}" across {scope}; closest sources: {found}'


_FILTER = """
    (CAST(:scheme AS text) IS NULL OR c.scheme = :scheme OR c.scheme = :general)
    AND (CAST(:include_platform AS boolean) OR s.doc_type <> 'platform_process')
    AND (CAST(:doc_types AS text[]) IS NULL OR s.doc_type = ANY(CAST(:doc_types AS text[])))
"""

# The query's words are ORed: questions rarely contain every word of the answer.
_SEARCH_SQL = text(f"""
    WITH q AS (
        SELECT NULLIF(replace(plainto_tsquery('english', :q)::text, '&', '|'), '') AS qs
    ),
    vec AS (
        SELECT c.id
        FROM corpus_chunk c JOIN corpus_source s ON s.id = c.source_id
        WHERE c.embedding IS NOT NULL AND {_FILTER}
        ORDER BY c.embedding <=> CAST(:qvec AS vector)
        LIMIT :limit
    ),
    txt AS (
        SELECT c.id,
               row_number() OVER (ORDER BY ts_rank_cd(c.tsv, to_tsquery('english', q.qs)) DESC)
                   AS trank
        FROM corpus_chunk c JOIN corpus_source s ON s.id = c.source_id, q
        WHERE q.qs IS NOT NULL AND c.tsv @@ to_tsquery('english', q.qs) AND {_FILTER}
        ORDER BY ts_rank_cd(c.tsv, to_tsquery('english', q.qs)) DESC
        LIMIT :limit
    )
    SELECT c.id::text AS id, c.text, c.section, c.scheme, c.page,
           s.key, s.url, s.title, s.publisher, s.doc_type, s.fetched_at,
           1 - (c.embedding <=> CAST(:qvec AS vector)) AS similarity,
           txt.trank AS text_rank
    FROM corpus_chunk c
    JOIN corpus_source s ON s.id = c.source_id
    LEFT JOIN txt ON txt.id = c.id
    WHERE c.id IN (SELECT id FROM vec UNION SELECT id FROM txt)
""")


def score(similarity: float, text_rank: int | None) -> float:
    if text_rank is not None and text_rank <= 3:
        return similarity + TEXT_BOOST_TOP3
    if text_rank is not None and text_rank <= 10:
        return similarity + TEXT_BOOST_TOP10
    return similarity


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"


class Retriever:
    def __init__(self, session: AsyncSession, embedder: Embedder) -> None:
        self._session = session
        self._embedder = embedder

    async def search(
        self,
        query: str,
        *,
        scheme: str | None = None,
        include_platform: bool = False,
        k: int = 4,
        doc_types: Sequence[str] | None = None,
    ) -> tuple[list[RetrievedChunk], SearchTrace]:
        """`doc_types`, when given, limits the search to those document types (for example
        the fee documents for the weekly fee explainer)."""
        params = {
            "q": query,
            "qvec": _vector_literal(self._embedder.embed_query(query)),
            "scheme": scheme,
            "general": GENERAL,
            "include_platform": include_platform,
            "limit": CANDIDATES,
            "doc_types": list(doc_types) if doc_types is not None else None,
        }
        rows = (await self._session.execute(_SEARCH_SQL, params)).all()
        ranked = sorted(rows, key=lambda r: -score(float(r.similarity), r.text_rank))[:k]
        results = [
            RetrievedChunk(
                chunk_id=r.id,
                source_key=r.key,
                url=r.url,
                title=r.title,
                publisher=r.publisher,
                doc_type=r.doc_type,
                scheme=r.scheme,
                section=r.section,
                page=r.page,
                text=r.text,
                fetched_at=r.fetched_at,
                similarity=float(r.similarity),
                text_rank=r.text_rank,
                score=score(float(r.similarity), r.text_rank),
            )
            for r in ranked
        ]
        trace = SearchTrace(
            query=query,
            scheme_filter=scheme,
            include_platform=include_platform,
            top=[
                {
                    "source": r.source_key,
                    "chunk_id": r.chunk_id,
                    "page": r.page,
                    "similarity": round(r.similarity, 3),
                    "text_rank": r.text_rank,
                }  # fmt: skip
                for r in results
            ],
        )
        return results, trace
