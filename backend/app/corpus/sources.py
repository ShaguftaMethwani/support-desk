"""Load and validate corpus/sources.yaml."""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

import yaml

from app.corpus.schemes import BY_SLUG, GENERAL
from app.policy.text import domain_allowed

CORPUS_DIR = Path(__file__).resolve().parents[2] / "corpus"
SOURCES_FILE = CORPUS_DIR / "sources.yaml"
MANUAL_DIR = CORPUS_DIR / "manual"


class Publisher(StrEnum):
    HDFC_MF = "hdfc_mf"
    SEBI = "sebi"
    AMFI = "amfi"
    INDMONEY_HELP = "indmoney_help"


DOC_TYPES = frozenset(
    {
        "kim", "sid", "sai", "factsheet", "fees", "faq", "statement_guide", "regulation",
        "education", "platform_process",
    }
)  # fmt: skip


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class Source:
    id: str
    url: str
    publisher: Publisher
    doc_type: str
    schemes: tuple[str, ...]
    fetch: str  # auto | manual
    file: str | None = None
    pages: tuple[tuple[int, int], ...] = ()

    @property
    def is_platform_process(self) -> bool:
        return self.doc_type == "platform_process"

    @property
    def manual_path(self) -> Path | None:
        return MANUAL_DIR / self.file if self.file else None

    def pending_reason(self) -> str | None:
        """Why a manual source can't be ingested yet, or None if it's ready."""
        if self.fetch != "manual":
            return None
        if not self.url:
            return "url not filled in"
        if self.manual_path is None or not self.manual_path.exists():
            return f"file not found: corpus/manual/{self.file}"
        return None

    def page_wanted(self, page_number: int) -> bool:
        return not self.pages or any(lo <= page_number <= hi for lo, hi in self.pages)


def load_sources(allowed_domains: list[str], path: Path = SOURCES_FILE) -> list[Source]:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)["sources"]
    sources: list[Source] = []
    seen: set[str] = set()
    for entry in raw:
        sid = entry["id"]
        if sid in seen:
            raise SourceError(f"duplicate source id {sid!r}")
        seen.add(sid)
        url = (entry.get("url") or "").strip()
        fetch = entry.get("fetch", "auto")
        if fetch not in ("auto", "manual"):
            raise SourceError(f"{sid}: fetch must be auto or manual")
        if fetch == "auto" and not url:
            raise SourceError(f"{sid}: automated sources need a url")
        if url and not domain_allowed(url, allowed_domains):
            raise SourceError(f"{sid}: {url} is not on the source allow-list")
        if fetch == "manual" and not entry.get("file"):
            raise SourceError(f"{sid}: manual sources need a file name")
        doc_type = entry["doc_type"]
        if doc_type not in DOC_TYPES:
            raise SourceError(f"{sid}: unknown doc_type {doc_type!r}")
        schemes = tuple(entry.get("schemes") or [GENERAL])
        for slug in schemes:
            if slug != GENERAL and slug not in BY_SLUG:
                raise SourceError(f"{sid}: unknown scheme {slug!r}")
        pages = tuple((int(lo), int(hi)) for lo, hi in entry.get("pages") or [])
        sources.append(
            Source(
                sid,
                url,
                Publisher(entry["publisher"]),
                doc_type,
                schemes,
                fetch,
                entry.get("file"),
                pages,
            )  # fmt: skip
        )
    return sources
