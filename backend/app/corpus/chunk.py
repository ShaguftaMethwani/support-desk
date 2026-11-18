"""Split parsed blocks into retrieval chunks and tag each with a scheme.

- About 350 tokens per chunk with ~50 tokens of overlap (a "token" here is a word or
  punctuation mark, a slight undercount of the embedding model's word pieces, which keeps
  chunks under its 512 limit).
- Table rows are never split. A chunk that starts mid-table repeats the table's header row.
- Every chunk starts with its section heading, and with the scheme name when it has one.
- Scheme tagging for documents that cover several schemes: each page starts with the scheme
  named near its top, and a block that opens with an HDFC scheme name switches the scheme.
  Pages and blocks about HDFC schemes outside the five in scope are dropped.
"""

import re
from collections.abc import Iterator
from dataclasses import dataclass

from app.corpus.parse import Block
from app.corpus.schemes import BY_SLUG, GENERAL, first_hdfc_scheme
from app.corpus.sources import Source

TARGET_TOKENS = 350
OVERLAP_TOKENS = 50
MIN_TOKENS = 40  # a new section starts a new chunk only after this much text
_OUT_OF_SCOPE = "__other__"
_TOKEN = re.compile(r"\w+|[^\w\s]")
_SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(])")


def count_tokens(text: str) -> int:
    return len(_TOKEN.findall(text))


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    scheme: str
    section: str | None
    page: int | None
    token_count: int


def _scheme_switch(block: Block) -> str | None:
    """A block opening with an HDFC scheme name switches context to that scheme."""
    slug, named = first_hdfc_scheme(block.text[:80])
    if not named or block.text.find("HDFC") > 20:
        return None
    return slug or _OUT_OF_SCOPE


def _tag_blocks(source: Source, blocks: list[Block]) -> Iterator[tuple[str, Block]]:
    if len(source.schemes) == 1:
        for block in blocks:
            yield source.schemes[0], block
        return
    allowed = set(source.schemes)
    context = GENERAL
    page: int | None = -1
    for i, block in enumerate(blocks):
        if block.page != page:
            page = block.page
            context = GENERAL
            head = " ".join(b.text for b in blocks[i : i + 5] if b.page == page)
            slug, named = first_hdfc_scheme(head)
            if named:
                context = slug if slug in allowed else _OUT_OF_SCOPE
        switch = _scheme_switch(block)
        if switch is not None:
            context = switch if switch in allowed else _OUT_OF_SCOPE
        if context != _OUT_OF_SCOPE:
            yield context, block


def _split_long(block: Block) -> list[Block]:
    if block.is_table_row or count_tokens(block.text) <= TARGET_TOKENS:
        return [block]
    parts, current = [], ""
    for sentence in _SENTENCE.split(block.text):
        candidate = f"{current} {sentence}".strip()
        if current and count_tokens(candidate) > TARGET_TOKENS:
            parts.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        parts.append(current)
    return [Block(p, block.page, block.is_heading, False, None) for p in parts]


def _render(scheme: str, section: str | None, blocks: list[Block]) -> str:
    lines: list[str] = []
    if scheme != GENERAL:
        lines.append(f"[{BY_SLUG[scheme].name}]")
    if section:
        lines.append(section)
    first = blocks[0]
    if first.is_table_row and first.table_header and first.text != first.table_header:
        lines.append(first.table_header)
    lines.extend(b.text for b in blocks if b.text != section)
    return "\n".join(lines)


def chunk_document(source: Source, blocks: list[Block]) -> list[Chunk]:
    chunks: list[Chunk] = []
    current: list[Block] = []
    current_scheme = GENERAL
    section: str | None = None
    current_section: str | None = None
    tokens = 0

    def flush(keep_overlap: bool) -> None:
        nonlocal current, tokens
        if not any(not b.is_heading for b in current):
            current = [] if not keep_overlap else current
            tokens = sum(count_tokens(b.text) for b in current)
            return
        text = _render(current_scheme, current_section, current)
        chunks.append(Chunk(len(chunks), text, current_scheme, current_section,
                            current[0].page, count_tokens(text)))  # fmt: skip
        carry: list[Block] = []
        if keep_overlap:
            carried = 0
            for block in reversed(current):
                size = count_tokens(block.text)
                if carried + size > OVERLAP_TOKENS:
                    break
                carry.insert(0, block)
                carried += size
        current = carry
        tokens = sum(count_tokens(b.text) for b in current)

    for scheme, block in _tag_blocks(source, blocks):
        if block.is_heading:
            section = block.text[:200]
        if current and (scheme != current_scheme or (block.is_heading and tokens >= MIN_TOKENS)):
            flush(keep_overlap=False)
        if not current:
            current_scheme, current_section = scheme, section
        for piece in _split_long(block):
            size = count_tokens(piece.text)
            if current and tokens + size > TARGET_TOKENS:
                flush(keep_overlap=True)
                current_scheme, current_section = scheme, section
            current.append(piece)
            tokens += size
    if current:
        flush(keep_overlap=False)
    return chunks
