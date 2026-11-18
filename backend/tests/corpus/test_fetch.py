from pathlib import Path

import httpx
import pytest

from app.corpus.fetch import USER_AGENT, Fetcher, FetchError
from app.corpus.sources import Publisher, Source


def source(url: str = "https://www.sebi.gov.in/docs/faq.pdf") -> Source:
    return Source("faq", url, Publisher.SEBI, "faq", ("general",), "auto")


def client(robots: str, requests: list[httpx.Request]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text=robots, headers={"content-type": "text/plain"})
        return httpx.Response(
            200, content=b"%PDF-1.4 test", headers={"content-type": "application/pdf"}
        )

    return httpx.Client(transport=httpx.MockTransport(handler), headers={"User-Agent": USER_AGENT})


def test_downloads_once_then_uses_cache(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    fetcher = Fetcher(client("User-agent: *\nDisallow: /js\n", requests), raw_dir=tmp_path)
    first = fetcher.get(source())
    second = Fetcher(client("", requests), raw_dir=tmp_path).get(source())
    assert first.content_type == "pdf" and second.content == first.content
    assert [r.url.path for r in requests] == ["/robots.txt", "/docs/faq.pdf"]
    assert all(r.headers["user-agent"] == USER_AGENT for r in requests)


def test_robots_disallow_is_respected(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    fetcher = Fetcher(client("User-agent: *\nDisallow: /docs/\n", requests), raw_dir=tmp_path)
    with pytest.raises(FetchError, match="robots.txt disallows"):
        fetcher.get(source())
    assert [r.url.path for r in requests] == ["/robots.txt"]


def test_manual_source_reads_the_downloaded_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.corpus.sources.MANUAL_DIR", tmp_path)
    (tmp_path / "kim.pdf").write_bytes(b"%PDF-1.4 kim")
    manual = Source(
        "kim",
        "https://www.hdfcfund.com/kim",
        Publisher.HDFC_MF,
        "kim",
        ("hdfc_liquid",),
        "manual",
        "kim.pdf",
    )
    document = Fetcher(client("", []), raw_dir=tmp_path).get(manual)
    assert document.content_type == "pdf" and document.content == b"%PDF-1.4 kim"


def test_pending_manual_source_raises(tmp_path: Path) -> None:
    manual = Source("kim", "", Publisher.HDFC_MF, "kim", ("hdfc_liquid",), "manual", "kim.pdf")
    with pytest.raises(FetchError, match="pending"):
        Fetcher(client("", []), raw_dir=tmp_path).get(manual)
