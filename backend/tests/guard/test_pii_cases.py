"""PII detection cases (Phase 1 asks for at least 60).

Each case is (text, expected PII types). An empty set means nothing should be redacted.
"""

import pytest

from app.guard import guard
from app.guard.verhoeff import check_digit


def aadhaar(base11: str) -> str:
    return base11 + check_digit(base11)


VALID_AADHAAR = aadhaar("23456789012")
VALID_AADHAAR_2 = aadhaar("98765432101")
INVALID_AADHAAR = VALID_AADHAAR[:-1] + str((int(VALID_AADHAAR[-1]) + 1) % 10)
G = lambda n: f"{n[:4]} {n[4:8]} {n[8:]}"  # noqa: E731
H = lambda n: f"{n[:4]}-{n[4:8]}-{n[8:]}"  # noqa: E731

CASES: list[tuple[str, set[str]]] = [
    # PAN
    ("ABCPD1234F", {"pan"}),
    ("my PAN is ABCDE1234F", {"pan"}),
    ("my pan is abcde1234f", {"pan"}),
    ("PAN: ABCPD 1234 F", {"pan"}),
    ("pan no. abcpd-1234-f please check", {"pan"}),
    ("A B C P D 1 2 3 4 F", {"pan"}),
    ("pan card number: bnzpm2501f.", {"pan"}),
    ("FGHPT9876Q is the PAN on my account", {"pan"}),
    ("company pan AAACH1234K", {"pan"}),
    ("abcpd1234f", {"pan"}),
    # Aadhaar
    (VALID_AADHAAR, {"aadhaar"}),
    (f"aadhaar {G(VALID_AADHAAR)}", {"aadhaar"}),
    (f"my uid is {H(VALID_AADHAAR_2)}", {"aadhaar"}),
    (f"Aadhar number {G(INVALID_AADHAAR)}", {"aadhaar"}),  # typo'd but labelled
    # Phone
    ("9876543210", {"phone"}),
    ("call me on +91 98765 43210", {"phone"}),
    ("+91-9876543210", {"phone"}),
    ("09876543210", {"phone"}),
    ("0091 9876543210", {"phone"}),
    ("98765-43210", {"phone"}),
    ("my number is 7012345678.", {"phone"}),
    ("6123456789 is my whatsapp", {"phone"}),
    ("987 654 3210", {"phone"}),
    # Email and UPI
    ("rahul.sharma@gmail.com", {"email"}),
    ("mail R_S+test@yahoo.co.in today", {"email"}),
    ("contact me at a.b@company.org.", {"email"}),
    ("pay rahul@okhdfcbank", {"upi"}),
    ("my upi 9876543210@ybl", {"upi"}),
    # Account, folio, demat
    ("account number 123456789012", {"account"}),
    ("a/c no: 50100123456789", {"account"}),
    ("folio 1234567/89", {"account"}),
    ("folio no. 98765432 shows the wrong NAV", {"account"}),
    ("demat IN30012345678901", {"account"}),
    ("bank account: 0123 4567 8901", {"account"}),
    ("client id 1203320000123456", {"account"}),
    # OTP and PIN
    ("OTP is 482913", {"otp"}),
    ("my otp 4829", {"otp"}),
    ("482913 is your OTP", {"otp"}),
    ("MPIN 4321", {"otp"}),
    ("verification code: 829301", {"otp"}),
    # Other long numbers
    ("ref 98765432101234", {"id_number"}),
    ("card 4111 1111 1111 1111", {"id_number"}),
    (INVALID_AADHAAR, {"id_number"}),  # 12 digits, bad checksum: still redacted, not as Aadhaar
    # Spoken forms (voice transcripts)
    ("nine eight seven six five four three two one zero", {"phone"}),
    ("my number is double nine eight seven six five four three two one", {"phone"}),
    ("plus nine one nine eight seven six five four three two one zero", {"phone"}),
    ("otp is four eight two nine one three", {"otp"}),
    ("rahul dot sharma at gmail dot com", {"email"}),
    ("A B C P D one two three four F", {"pan"}),
    ("call me at nine eight seven, six five four, three two one zero", {"phone"}),
    # Several at once
    ("PAN ABCPD1234F, phone 9876543210, mail x.y@gmail.com", {"pan", "phone", "email"}),
    # Must NOT be redacted
    ("SIP of 5000 every month", set()),
    ("The exit load is 1% within 365 days", set()),
    ("NAV is 123.4567 as of today", set()),
    ("scheme code 119551", set()),
    ("PIN code 400001", set()),
    ("In 2024 the OTP rules changed", set()),
    ("my account statement for 2024", set()),
    ("my account was opened in 2019", set()),
    ("OTP not received", set()),
    ("5876543210", set()),  # 10 digits but not a valid mobile prefix
    (f"ref {G(INVALID_AADHAAR)}", set()),  # grouped, bad checksum, no label
    ("March 2024 a new circular came", set()),
    ("SEBI and AMFI rules for ELSS lock-in", set()),
    ("I have one question about two funds", set()),
    ("email us via the contact page", set()),
    ("follow @hdfcfund for updates", set()),
    ("₹1,00,000 invested in HDFC Liquid Fund", set()),
    ("ISIN INF179K01BB8 is listed in the SID", set()),
]


def test_case_count_meets_plan() -> None:
    assert len(CASES) >= 60


@pytest.mark.parametrize(("text", "expected"), CASES)
def test_pii_detection(text: str, expected: set[str]) -> None:
    result = guard(text, names=False)
    assert set(result.pii_flags) == expected
    if expected:
        assert "_REDACTED]" in result.text
    else:
        assert result.text == text
