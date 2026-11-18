"""Build the corpus.

Run from backend/:
    uv run python -m corpus.ingest              # ingest every ready source
    uv run python -m corpus.ingest --refresh    # re-download automated sources first
    uv run python -m corpus.ingest --only sebi_mf_faq hdfc_sai
    uv run python -m corpus.ingest --list       # print the source list as a Markdown table

Manual sources (hdfcfund.com blocks automated access) are skipped until their file is in
corpus/manual/ and their exact URL is filled in, and are reported as pending.
"""

import argparse
import asyncio
import collections
import sys

from app.config import get_settings
from app.corpus.chunk import chunk_document
from app.corpus.embed import Embedder, FastEmbedder
from app.corpus.fetch import Fetcher, FetchError
from app.corpus.parse import parse
from app.corpus.sources import Source, load_sources
from app.corpus.store import replace_source
from app.db.session import get_sessionmaker


def print_source_table(sources: list[Source]) -> None:
    print("| Source | Publisher | Type | Schemes | URL |")
    print("|---|---|---|---|---|")
    for s in sources:
        url = s.url or "(pending manual download)"
        print(f"| {s.id} | {s.publisher.value} | {s.doc_type} | {', '.join(s.schemes)} | {url} |")


async def ingest(sources: list[Source], *, refresh: bool, embedder: Embedder) -> int:
    fetcher = Fetcher()
    sessions = get_sessionmaker()
    failures = 0
    print(f"{'source':26} {'kind':5} {'chunks':>6}  schemes")
    for source in sources:
        pending = source.pending_reason()
        if pending:
            print(f"{source.id:26} PENDING: {pending}")
            continue
        try:
            document = fetcher.get(source, refresh=refresh)
        except FetchError as exc:
            print(f"{source.id:26} FAILED: {exc}")
            failures += 1
            continue
        parsed = parse(document.content, document.content_type, source.page_wanted)
        chunks = chunk_document(source, parsed.blocks)
        if not chunks:
            print(f"{source.id:26} FAILED: no text extracted")
            failures += 1
            continue
        embeddings = embedder.embed_documents([c.text for c in chunks])
        async with sessions() as session:
            await replace_source(
                session,
                source,
                title=parsed.title,
                fetched_at=document.fetched_at,
                sha256=document.sha256,
                chunks=chunks,
                embeddings=embeddings,
            )
            await session.commit()
        schemes = collections.Counter(c.scheme for c in chunks)
        print(f"{source.id:26} {document.content_type:5} {len(chunks):6}  {dict(schemes)}")
    return failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("--refresh", action="store_true", help="re-download automated sources")
    parser.add_argument("--only", nargs="+", metavar="ID", help="ingest only these source ids")
    parser.add_argument("--list", action="store_true", help="print the source list and exit")
    args = parser.parse_args(argv)

    sources = load_sources(get_settings().source_domains)
    if args.only:
        unknown = set(args.only) - {s.id for s in sources}
        if unknown:
            parser.error(f"unknown source ids: {', '.join(sorted(unknown))}")
        sources = [s for s in sources if s.id in args.only]
    if args.list:
        print_source_table(sources)
        return 0
    failures = asyncio.run(ingest(sources, refresh=args.refresh, embedder=FastEmbedder()))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
