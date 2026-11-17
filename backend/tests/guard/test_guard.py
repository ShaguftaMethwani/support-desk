import re

import pytest

from app.guard import find_pii, guard, redact_structured
from app.guard.spoken import normalise_spoken
from app.guard.types import PiiType
from app.guard.verhoeff import check_digit, is_valid

SECRETS = ["ABCPD1234F", "9876543210", "x.y@gmail.com", "482913"]
MESSAGE = "PAN ABCPD1234F, phone 9876543210, mail x.y@gmail.com, OTP is 482913"


def test_values_never_survive_redaction() -> None:
    result = guard(MESSAGE)
    for secret in SECRETS:
        assert secret not in result.text
    assert result.redaction_count == 4
    assert result.pii_flags == ["email", "otp", "pan", "phone"]


def test_guard_is_idempotent() -> None:
    once = guard(MESSAGE)
    twice = guard(once.text)
    assert twice.text == once.text
    assert twice.pii_flags == []


def test_tokens_are_typed() -> None:
    assert guard("my pan is ABCPD1234F").text == "my pan is [PAN_REDACTED]"
    assert PiiType.AADHAAR.token == "[AADHAAR_REDACTED]"


def test_find_pii_reports_types_only() -> None:
    assert find_pii("call 9876543210") == [PiiType.PHONE]
    assert find_pii("nothing here") == []


def test_structured_redaction_skips_names_and_counts() -> None:
    clean, count = redact_structured("John Smith 9876543210")
    assert clean == "John Smith [PHONE_REDACTED]"
    assert count == 1


def test_overlapping_matches_keep_the_longer_span() -> None:
    # The phone number sits inside a UPI id. The whole UPI id is redacted once.
    result = guard("pay 9876543210@ybl", names=False)
    assert result.text == "pay [UPI_REDACTED]"
    assert result.pii_flags == ["upi"]


@pytest.mark.parametrize(
    ("spoken", "written"),
    [
        ("nine eight seven six five four three two one zero", "9876543210"),
        ("double five six seven", "5567"),
        ("triple zero one", "0001"),
        ("plus nine one nine eight seven six five four three two one zero", "+919876543210"),
        ("rahul dot sharma at gmail dot com", "rahul.sharma@gmail.com"),
        ("priya underscore k at the rate yahoo dot co dot in", "priya_k@yahoo.co.in"),
        ("98765 four three two one zero", "9876543210"),
        ("I have one question about two funds", "I have one question about two funds"),
        ("one or two", "one or two"),
        ("oh no", "oh no"),
        ("SIP of 5000 and 2000", "SIP of 5000 and 2000"),
    ],
)
def test_spoken_normalisation(spoken: str, written: str) -> None:
    assert normalise_spoken(spoken) == written


def test_verhoeff() -> None:
    assert check_digit("236") == "3"
    assert is_valid("2363")
    assert not is_valid("2364")
    assert not is_valid("12a4")


@pytest.mark.parametrize(
    ("text", "redacted"),
    [
        ("My name is Rahul Sharma and I need help", "My name is [NAME_REDACTED] and I need help"),
        ("my name is priya", "my name is [NAME_REDACTED]"),
        ("name: anita desai", "name: [NAME_REDACTED]"),
        ("Hi, this is Anita from Pune.", "Hi, this is [NAME_REDACTED] from Pune."),
        ("John Smith called about the exit load", "[NAME_REDACTED] called about the exit load"),
    ],
)
def test_names_are_redacted_at_ingress(text: str, redacted: str) -> None:
    result = guard(text)
    assert result.text == redacted
    assert "name" in result.pii_flags


@pytest.mark.parametrize(
    "text",
    [
        "I am confused about the exit load",
        "HDFC Liquid Fund exit load question",
        "SEBI and AMFI rules for ELSS",
        "This is about my SIP in HDFC Flexi Cap",
        "folio 1234567/89 has an issue",
    ],
)
def test_domain_terms_are_not_names(text: str) -> None:
    assert "name" not in guard(text).pii_flags


def test_names_are_not_checked_by_default_in_find_pii() -> None:
    # Outgoing drafts may name a fund manager. Only structured PII blocks them.
    assert find_pii("The fund manager is Roshi Jain.") == []
    assert find_pii("The fund manager is Roshi Jain.", names=True) == [PiiType.NAME]


def test_redaction_tokens_contain_no_digits() -> None:
    for t in PiiType:
        assert not re.search(r"\d", t.token)
