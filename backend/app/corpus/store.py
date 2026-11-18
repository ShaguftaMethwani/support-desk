"""Write sources and chunks to Postgres. Re-ingesting a source replaces its chunks."""

from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.corpus.chunk import Chunk
from app.corpus.sources import Source
from app.db.models import CorpusChunk, CorpusSource


async def replace_source(
    session: AsyncSession,
    source: Source,
    *,
    title: str | None,
    fetched_at: datetime,
    sha256: str,
    chunks: list[Chunk],
    embeddings: list[list[float]],
) -> CorpusSource:
    if len(chunks) != len(embeddings):
        raise ValueError("one embedding per chunk is required")
    row = await session.scalar(select(CorpusSource).where(CorpusSource.key == source.id))
    if row is None:
        row = CorpusSource(key=source.id, url=source.url)
        session.add(row)
    row.url = source.url
    row.publisher = source.publisher.value
    row.doc_type = source.doc_type
    row.title = title
    row.schemes = list(source.schemes)
    row.fetched_at = fetched_at
    row.sha256 = sha256
    await session.flush()
    await session.execute(delete(CorpusChunk).where(CorpusChunk.source_id == row.id))
    session.add_all(
        CorpusChunk(
            source_id=row.id,
            chunk_index=chunk.index,
            text=chunk.text,
            section=chunk.section,
            scheme=chunk.scheme,
            page=chunk.page,
            token_count=chunk.token_count,
            embedding=vector,
        )
        for chunk, vector in zip(chunks, embeddings, strict=True)
    )
    await session.flush()
    return row
