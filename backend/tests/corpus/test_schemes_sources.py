from pathlib import Path

import pytest

from app.corpus.schemes import find_schemes, first_hdfc_scheme
from app.corpus.sources import SourceError, load_sources

ALLOWED = ["hdfcfund.com", "sebi.gov.in", "amfiindia.com", "indmoney.com"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("What is the exit load on HDFC Liquid Fund?", ["hdfc_liquid"]),
        ("HDFC Mid-Cap Opportunities Fund benchmark", ["hdfc_mid_cap"]),
        ("elss lock in", ["hdfc_elss_tax_saver"]),
        ("compare flexi cap and large cap", ["hdfc_flexi_cap", "hdfc_large_cap"]),
        ("minimum SIP for HDFC ELSS - Tax Saver Fund", ["hdfc_elss_tax_saver"]),
        ("what is a mutual fund", []),
    ],
)
def test_find_schemes(text: str, expected: list[str]) -> None:
    assert find_schemes(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HDFC Mid Cap Fund", ("hdfc_mid_cap", True)),
        ("For HDFC ELSS Tax saver:", ("hdfc_elss_tax_saver", True)),
        ("HDFC Liquid Fund - Direct Plan", ("hdfc_liquid", True)),
        ("HDFC Top 100 Fund", ("hdfc_large_cap", True)),
        # Contains "Midcap" but is a different scheme.
        ("HDFC NIFTY Midcap 150 Index Fund", (None, True)),
        ("HDFC Large and Mid Cap Fund", (None, True)),
        ("HDFC Banking & Financial Services Fund", (None, True)),
        ("SEBI rules", (None, False)),
    ],
)
def test_first_hdfc_scheme_requires_a_full_name_match(text: str, expected: tuple) -> None:  # type: ignore[type-arg]
    assert first_hdfc_scheme(text) == expected


def write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "sources.yaml"
    path.write_text("sources:\n" + body)
    return path


def test_real_source_list_is_valid() -> None:
    sources = load_sources(ALLOWED)
    assert 15 <= len(sources) <= 25
    assert {s.publisher.value for s in sources} >= {"hdfc_mf", "sebi", "amfi"}


def test_off_list_domain_is_refused(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
  - {id: blog, url: https://someblog.com/x, publisher: amfi, doc_type: faq, fetch: auto}
""",
    )
    with pytest.raises(SourceError, match="allow-list"):
        load_sources(ALLOWED, path)


def test_manual_source_without_url_is_pending(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        """
  - {id: kim, url: "", publisher: hdfc_mf, doc_type: kim, schemes: [hdfc_liquid],
     fetch: manual, file: missing.pdf}
""",
    )
    (source,) = load_sources(ALLOWED, path)
    assert source.pending_reason() == "url not filled in"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            "  - {id: a, url: https://www.sebi.gov.in/a, publisher: sebi, doc_type: faq}\n"
            "  - {id: a, url: https://www.sebi.gov.in/b, publisher: sebi, doc_type: faq}\n",
            "duplicate",
        ),
        (
            "  - {id: a, url: https://www.sebi.gov.in/a, publisher: sebi, doc_type: poem}\n",
            "doc_type",
        ),
        (
            "  - {id: a, url: https://www.sebi.gov.in/a, publisher: sebi, doc_type: faq,"
            " schemes: [hdfc_small_cap]}\n",
            "unknown scheme",
        ),
        ("  - {id: a, url: '', publisher: sebi, doc_type: faq, fetch: auto}\n", "need a url"),
    ],
)
def test_invalid_entries_are_rejected(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(SourceError, match=message):
        load_sources(ALLOWED, write(tmp_path, body))


def test_page_ranges() -> None:
    sai = next(s for s in load_sources(ALLOWED) if s.id == "hdfc_sai")
    assert sai.page_wanted(62) and not sai.page_wanted(100)
