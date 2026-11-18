"""Download automated sources politely, and read manual ones from disk.

- Only allow-listed domains (checked when sources are loaded).
- robots.txt is respected. A disallowed URL is refused, not worked around.
- An honest User-Agent that identifies the project. No browser impersonation.
- One request per second per host, and a local cache so re-runs don't re-download.
- Certificates are verified against the operating system's trust store (truststore).
  Several SEBI and AMFI hosts don't verify against Python's bundled list, but do against the
  system store. Verification is never turned off.
"""

import hashlib
import json
import ssl
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx
import truststore

from app.corpus.sources import CORPUS_DIR, Source

RAW_DIR = CORPUS_DIR / "raw"
USER_AGENT = "SupportDeskCorpusBot/0.1 (student project; fetches public investor documents)"
_MIN_INTERVAL_S = 1.0


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Document:
    source: Source
    content: bytes
    content_type: str  # pdf | html | xlsx
    fetched_at: datetime
    sha256: str


def _kind(content_type: str, url_or_name: str, content: bytes) -> str:
    if "pdf" in content_type or url_or_name.lower().endswith(".pdf") or content[:5] == b"%PDF-":
        return "pdf"
    # Office Open XML spreadsheets are zip files. Judged by content, because sites often serve
    # them with a ".xls" name.
    if content[:4] == b"PK\x03\x04" and (
        "spreadsheetml" in content_type or url_or_name.lower().endswith((".xlsx", ".xls"))
    ):
        return "xlsx"
    return "html"


class Fetcher:
    def __init__(self, client: httpx.Client | None = None, raw_dir: Path = RAW_DIR) -> None:
        self._client = client or httpx.Client(
            headers={"User-Agent": USER_AGENT},
            timeout=60,
            follow_redirects=True,
            verify=truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT),
        )
        self._raw_dir = raw_dir
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_request: dict[str, float] = {}

    def _wait_turn(self, host: str) -> None:
        elapsed = time.monotonic() - self._last_request.get(host, 0.0)
        if elapsed < _MIN_INTERVAL_S:
            time.sleep(_MIN_INTERVAL_S - elapsed)
        self._last_request[host] = time.monotonic()

    def _robots_for(self, url: str) -> RobotFileParser | None:
        parts = urlsplit(url)
        host = f"{parts.scheme}://{parts.netloc}"
        if host not in self._robots:
            self._wait_turn(parts.netloc)
            response = self._client.get(f"{host}/robots.txt")
            if response.status_code == 200 and "text/plain" in response.headers.get(
                "content-type", ""
            ):
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
                self._robots[host] = parser
            else:
                self._robots[host] = None  # no robots.txt: no restrictions declared
        return self._robots[host]

    def allowed_by_robots(self, url: str) -> bool:
        robots = self._robots_for(url)
        return robots is None or robots.can_fetch(USER_AGENT, url)

    def _cache_paths(self, source: Source) -> tuple[Path, Path]:
        return self._raw_dir / f"{source.id}.bin", self._raw_dir / f"{source.id}.json"

    def get(self, source: Source, *, refresh: bool = False) -> Document:
        if source.fetch == "manual":
            return self._read_manual(source)
        body_path, meta_path = self._cache_paths(source)
        if not refresh and body_path.exists() and meta_path.exists():
            meta = json.loads(meta_path.read_text())
            content = body_path.read_bytes()
            return Document(source, content, meta["kind"],
                            datetime.fromisoformat(meta["fetched_at"]), meta["sha256"])  # fmt: skip
        return self._download(source)

    def _download(self, source: Source) -> Document:
        if not self.allowed_by_robots(source.url):
            raise FetchError(f"{source.id}: robots.txt disallows {source.url}")
        self._wait_turn(urlsplit(source.url).netloc)
        response = self._client.get(source.url)
        if response.status_code != 200:
            raise FetchError(f"{source.id}: HTTP {response.status_code} from {source.url}")
        content = response.content
        kind = _kind(response.headers.get("content-type", ""), source.url, content)
        fetched_at = datetime.now(UTC)
        digest = hashlib.sha256(content).hexdigest()
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        body_path, meta_path = self._cache_paths(source)
        body_path.write_bytes(content)
        meta_path.write_text(json.dumps(
            {"url": source.url, "kind": kind, "fetched_at": fetched_at.isoformat(),
             "sha256": digest}, indent=2))  # fmt: skip
        return Document(source, content, kind, fetched_at, digest)

    def _read_manual(self, source: Source) -> Document:
        reason = source.pending_reason()
        if reason:
            raise FetchError(f"{source.id}: manual source pending ({reason})")
        path = source.manual_path
        assert path is not None
        content = path.read_bytes()
        # Last checked = when the file was downloaded by hand.
        fetched_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
        kind = _kind("", path.name, content)
        return Document(source, content, kind, fetched_at, hashlib.sha256(content).hexdigest())
