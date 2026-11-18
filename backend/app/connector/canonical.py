"""Canonical explainers in the response loop (architecture section 9.2).

An approved `promote_canonical` action makes a weekly fee explainer the canonical answer for its
fee type (app/gate/service.py). This module finds the one that fits a question and builds the
reply from it:

- **Which explainer.** Only active ones. A question that names a fee type only ever gets that
  fee type's explainer: always for `fee_canonical`, and for a factual question or complaint
  when it is also close enough. A question that names no fee type gets the nearest explainer
  if it is close enough (a lower bar for `fee_canonical`). A factual question or complaint
  that names a scheme never uses one: a scheme's own figure comes from its own documents.
- **The reply.** Code picks the 1 to 3 bullets nearest the question, all from the source of the
  nearest bullet, so the one citation covers every sentence. No model writes or rewords them.
  The citation is the corpus chunk that bullet came from, with that chunk's Last checked date.
"""

import asyncio
import uuid
from dataclasses import dataclass

import numpy as np
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.facts import SourceCitation, unsupported_numbers
from app.config import Settings
from app.connector.clusters import vector_literal
from app.corpus.embed import Embedder
from app.db.models import CanonicalExplainer, CorpusChunk, CorpusSource, FeeExplainer
from app.policy.text import count_sentences
from app.policy.validators import REPLY_MAX_SENTENCES

MAX_BULLETS = 3
# A second or third bullet joins the reply only if it is about as relevant as the first.
BULLET_MARGIN = 0.08
CANONICAL_INTENTS = frozenset({"fee_canonical", "factual", "complaint"})


@dataclass(frozen=True)
class CanonicalAnswer:
    canonical_id: uuid.UUID
    fee_type: str
    body: str
    citation: SourceCitation
    bullets: list[int]  # indexes into the explainer's bullets, in reply order
    similarity: float  # question to explainer
    matched_by: str  # fee_type | embedding


@dataclass(frozen=True)
class _Match:
    canonical: CanonicalExplainer
    explainer: FeeExplainer
    similarity: float
    matched_by: str


def _unit(rows: list[list[float]]) -> np.ndarray:
    m = np.asarray(rows, dtype=np.float64)
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


async def _candidates(
    s: AsyncSession, embedding: list[float]
) -> list[tuple[CanonicalExplainer, float]]:
    rows = (
        await s.execute(
            text(
                "SELECT id, 1 - (embedding <=> CAST(:v AS vector)) AS sim "
                "FROM canonical_explainer WHERE active AND embedding IS NOT NULL "
                "ORDER BY embedding <=> CAST(:v AS vector)"
            ),
            {"v": vector_literal(embedding)},
        )
    ).all()
    out = []
    for row in rows:
        canonical = await s.get(CanonicalExplainer, row.id)
        assert canonical is not None
        out.append((canonical, float(row.sim)))
    return out


async def find_canonical(
    s: AsyncSession,
    *,
    embedding: list[float],
    intent: str,
    scheme: str | None,
    fee_type: str | None,
    settings: Settings,
) -> _Match | None:
    if intent not in CANONICAL_INTENTS or not embedding:
        return None
    if intent != "fee_canonical" and scheme is not None:
        return None
    candidates = await _candidates(s, embedding)
    if not candidates:
        return None
    match: tuple[CanonicalExplainer, float, str] | None = None
    if fee_type is not None:
        # A question that names a fee type only ever gets that fee type's explainer.
        by_type = next(((c, sim) for c, sim in candidates if c.fee_type == fee_type), None)
        if by_type is not None and (
            intent == "fee_canonical" or by_type[1] >= settings.canonical_fee_type_similarity
        ):
            match = (by_type[0], by_type[1], "fee_type")
    else:
        nearest, sim = candidates[0]
        floor = (
            settings.canonical_fee_similarity
            if intent == "fee_canonical"
            else settings.canonical_similarity
        )
        if sim >= floor:
            match = (nearest, sim, "embedding")
    if match is None:
        return None
    explainer = await s.get(FeeExplainer, match[0].explainer_id)
    if explainer is None or not explainer.bullets:
        return None
    return _Match(match[0], explainer, match[1], match[2])


async def _chunks(s: AsyncSession, ids: list[str]) -> dict[str, tuple[CorpusChunk, CorpusSource]]:
    valid = []
    for i in ids:
        try:
            valid.append(uuid.UUID(i))
        except (TypeError, ValueError):
            continue
    if not valid:
        return {}
    rows = await s.execute(
        select(CorpusChunk, CorpusSource)
        .join(CorpusSource, CorpusSource.id == CorpusChunk.source_id)
        .where(CorpusChunk.id.in_(valid))
    )
    return {str(c.id): (c, src) for c, src in rows.all()}


def _citation(chunk: CorpusChunk, source: CorpusSource) -> SourceCitation:
    fetched = source.fetched_at
    return SourceCitation(
        chunk_id=str(chunk.id),
        source_key=source.key,
        url=source.url,
        title=source.title,
        page=chunk.page,
        last_checked=fetched.date() if fetched else None,
    )


async def build_answer(
    s: AsyncSession, embedder: Embedder, question_embedding: list[float], match: _Match
) -> CanonicalAnswer | None:
    bullets = [str(b) for b in match.explainer.bullets]
    chunk_ids = [str(c) for c in (match.canonical.bullet_chunk_ids or [])]
    if len(chunk_ids) != len(bullets):
        return None  # can't cite it; the caller answers from the corpus instead
    chunks = await _chunks(s, chunk_ids)
    vectors = await asyncio.to_thread(embedder.embed_documents, bullets)
    sims = _unit(vectors) @ _unit([question_embedding])[0]
    order = [int(i) for i in np.argsort(-sims, kind="stable") if chunk_ids[int(i)] in chunks]
    if not order:
        return None
    top = order[0]
    url = chunks[chunk_ids[top]][1].url
    chosen = [top]
    for i in order[1:]:
        if len(chosen) == MAX_BULLETS or sims[i] < sims[top] - BULLET_MARGIN:
            break
        if chunks[chunk_ids[i]][1].url != url:
            continue
        candidate = sorted([*chosen, i])
        if count_sentences(" ".join(bullets[j] for j in candidate)) > REPLY_MAX_SENTENCES:
            break
        chosen.append(i)
    chosen.sort()  # the explainer's own order reads best
    body = " ".join(bullets[i].rstrip() for i in chosen)
    if count_sentences(body) > REPLY_MAX_SENTENCES:
        return None
    chunk, source = chunks[chunk_ids[top]]
    return CanonicalAnswer(
        canonical_id=match.canonical.id,
        fee_type=match.canonical.fee_type,
        body=body,
        citation=_citation(chunk, source),
        bullets=chosen,
        similarity=match.similarity,
        matched_by=match.matched_by,
    )


async def canonical_for_question(
    s: AsyncSession,
    embedder: Embedder,
    *,
    embedding: list[float],
    intent: str,
    scheme: str | None,
    fee_type: str | None,
    settings: Settings,
) -> CanonicalAnswer | None:
    match = await find_canonical(
        s, embedding=embedding, intent=intent, scheme=scheme, fee_type=fee_type, settings=settings
    )
    if match is None:
        return None
    return await build_answer(s, embedder, embedding, match)


async def count_reuse(s: AsyncSession, canonical_id: uuid.UUID) -> None:
    await s.execute(
        update(CanonicalExplainer)
        .where(CanonicalExplainer.id == canonical_id)
        .values(reuse_count=CanonicalExplainer.reuse_count + 1)
    )


# --- promotion ------------------------------------------------------------------------------


async def bullet_chunk_ids(
    s: AsyncSession, explainer: FeeExplainer, embedder: Embedder
) -> list[str]:
    """The corpus chunk behind each bullet. Explainers from Phase 6 on store them; older ones
    are matched to the nearest chunk of their two sources that holds every figure the bullet
    quotes. A bullet with no such chunk gets "" and is never used in a reply."""
    stored = [str(c) for c in (explainer.chunk_ids or [])]
    if len(stored) == len(explainer.bullets) and all(stored):
        return stored
    rows = (
        (
            await s.execute(
                select(CorpusChunk)
                .join(CorpusSource, CorpusSource.id == CorpusChunk.source_id)
                .where(CorpusSource.url.in_(list(explainer.source_urls)))
                .where(CorpusChunk.embedding.is_not(None))
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return ["" for _ in explainer.bullets]
    bullets = [str(b) for b in explainer.bullets]
    vectors = await asyncio.to_thread(embedder.embed_documents, bullets)
    chunk_m = _unit([list(c.embedding) for c in rows])  # type: ignore[arg-type]
    out = []
    for bullet, sims in zip(bullets, _unit(vectors) @ chunk_m.T, strict=True):
        best = ""
        for i in np.argsort(-sims, kind="stable"):
            chunk = rows[int(i)]
            if not unsupported_numbers(bullet, chunk.text):
                best = str(chunk.id)
                break
        out.append(best)
    return out
