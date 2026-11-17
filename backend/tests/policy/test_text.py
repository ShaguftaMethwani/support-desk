import pytest

from app.policy.text import count_bullets, count_sentences, count_words, domain_allowed, find_urls


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Rs. 500 is the minimum. E.g. ELSS needs 3 years.", 2),
        ("The exit load is 1.5% within 1 year. After that it is nil.", 2),
        ("Visit https://www.hdfcfund.com/a.b/c.pdf for details. Thanks.", 2),
        ("Expense ratio is approx. 0.7% p.a. for the direct plan.", 1),
        ("One. Two! Three? Four.", 4),
        ("Line one\nLine two", 2),
        ("Units are locked in for 3 years.\nLast checked: 2026-11-17", 1),
        ("Lock-in is 3 years.\nSource: https://www.amfiindia.com/elss", 1),
        ("i.e. the Direct Plan.", 1),
        ("Mr. Shah manages it. Write to help@hdfcfund.com for help.", 2),
        ("Wait... what about it?", 1),
        ("The fund is from HDFC Mutual Fund Ltd. It is open-ended.", 2),
        ("HDFC AMC Ltd. is the sponsor.", 1),
        ("U.S. markets are closed. Fine.", 2),
        ("Charges include STT, stamp duty etc. The exit load is separate.", 2),
        ("No. of units is shown in the statement.", 1),
        ("", 0),
    ],
)
def test_sentence_counting(text: str, expected: int) -> None:
    assert count_sentences(text) == expected


def test_word_and_bullet_counting() -> None:
    assert count_words("The minimum SIP is ₹100 and exit load is 1%.") == 10
    assert count_words("Lock-in: 3 years (ELSS).") == 4
    assert count_bullets("- a\n- b\n* c\n1. d\n2) e\ntext\n• f") == 6


def test_urls_and_domains() -> None:
    allowed = ["hdfcfund.com", "sebi.gov.in"]
    assert find_urls("See https://www.sebi.gov.in/x.html. Thanks") == [
        "https://www.sebi.gov.in/x.html"
    ]
    assert domain_allowed("https://www.hdfcfund.com/factsheet.pdf", allowed)
    assert domain_allowed("https://sebi.gov.in/a", allowed)
    assert not domain_allowed("https://hdfcfund.com.evil.example/a", allowed)
    assert not domain_allowed("https://notsebi.gov.in/a", allowed)
    assert not domain_allowed("ftp://hdfcfund.com/a", allowed)
    assert not domain_allowed("https://someblog.com/hdfcfund.com", allowed)
