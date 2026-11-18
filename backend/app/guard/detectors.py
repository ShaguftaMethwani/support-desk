"""Structured PII detectors (architecture section 6.1). Pure regex, no model.

These run everywhere: at ingress, on every outgoing draft, in the database write guard
and on every log line. Person names are handled separately in names.py and only at ingress.
"""

import re
from collections.abc import Callable, Iterator

from app.guard import verhoeff
from app.guard.types import PiiMatch, PiiType

_NOT_ALNUM_BEFORE = r"(?<![A-Za-z0-9])"
_NOT_ALNUM_AFTER = r"(?![A-Za-z0-9])"
_YEAR = re.compile(r"(?:19|20)\d\d")

# --- PAN: 5 letters, 4 digits, 1 letter. The 4th letter is the holder type. -----------------
_PAN_HOLDER_TYPES = "PCHFATBLJG"
# Uppercase, separators optional: "ABCDE1234F", "ABCPD 1234 F", "A B C P D 1 2 3 4 F".
_PAN_UPPER = re.compile(
    _NOT_ALNUM_BEFORE + r"[A-Z](?:[ -]?[A-Z]){4}(?:[ -]?\d){4}[ -]?[A-Z]" + _NOT_ALNUM_AFTER
)
# Any case, contiguous, valid holder type: "abcpd1234f".
_PAN_ANY_CASE = re.compile(
    _NOT_ALNUM_BEFORE + rf"[A-Za-z]{{3}}[{_PAN_HOLDER_TYPES}{_PAN_HOLDER_TYPES.lower()}]"
    r"[A-Za-z]\d{4}[A-Za-z]" + _NOT_ALNUM_AFTER
)
# Run into the next word by speech-to-text: "ABCPE1234F. Why" heard as "ABCPE1234FY". The
# 4th letter must be a valid holder type; the whole token is redacted.
_PAN_GLUED = re.compile(
    _NOT_ALNUM_BEFORE + rf"[A-Za-z]{{3}}[{_PAN_HOLDER_TYPES}{_PAN_HOLDER_TYPES.lower()}]"
    r"[A-Za-z]\d{4}[A-Za-z]{2,4}" + _NOT_ALNUM_AFTER
)
# After the word "PAN", any case and separators: "my pan is abcde 1234 f".
_PAN_AFTER_KEYWORD = re.compile(
    r"(?i)\bpan(?:\s*(?:card|no\.?|num(?:ber)?|#))*(?![A-Za-z])(?:\s*(?:is|:|-|=)\s*|\s+)"
    r"([a-z](?:[ -]?[a-z]){4}(?:[ -]?\d){4}[ -]?[a-z])" + _NOT_ALNUM_AFTER
)

# --- Aadhaar: 12 digits, first digit 2-9, Verhoeff checksum ----------------------------------
_AADHAAR = re.compile(r"(?<![\d])([2-9]\d{3})([ -]?)(\d{4})\2(\d{4})(?![\d])")
_AADHAAR_KEYWORD = re.compile(r"(?i)\b(?:aadhaa?r|aadhar|uidai|uid)\b")

# --- Indian mobile numbers ------------------------------------------------------------------
_PHONE = re.compile(r"(?<![\w+])(?:(?:\+|00)91[ -]?|91[ -]|0)?[6-9](?:[ -]?\d){9}(?!\d)")

# --- Email and UPI ids ----------------------------------------------------------------------
_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![\w-])"
)
_UPI = re.compile(r"(?<![\w.+-])[A-Za-z0-9._-]{2,256}@[A-Za-z][A-Za-z0-9]{1,63}(?![\w.@-])")

# --- Account, folio and demat numbers near a keyword ----------------------------------------
_ACCOUNT = re.compile(
    r"(?i)\b(?:a/c|acct|account|acc(?:ount)?\s*(?:no|number|#)|folio|demat|client\s*id|"
    r"dp\s*id|customer\s*id|crn|ifsc\s*and\s*account)\b[^\d\n]{0,30}?"
    r"(\d(?:[ /-]?\d){5,17})(?![\d])"
)
_DEMAT_NSDL = re.compile(_NOT_ALNUM_BEFORE + r"IN\d{14}" + _NOT_ALNUM_AFTER)

# --- OTPs and PINs near a keyword -----------------------------------------------------------
_OTP_WORDS = (
    r"(?:otp|one[- ]time[- ]password|verification\s+code|security\s+code|"
    r"auth(?:entication)?\s+code|passcode|[mt]?pin(?!\s*code))"
)
_OTP_AFTER = re.compile(rf"(?i)\b{_OTP_WORDS}\b[^\d\n]{{0,25}}?(?<!\d)(\d{{4,8}})(?!\d)")
_OTP_BEFORE = re.compile(rf"(?i)(?<![\d.,])(\d{{4,8}})(?!\d)[^\d\n]{{0,25}}?\b{_OTP_WORDS}\b")

# --- Other long numbers: card numbers and unexplained ids -----------------------------------
_LONG_NUMBER = re.compile(r"(?<![\w.,])\d{11,18}(?![\w.,])")
_CARD = re.compile(r"(?<![\d])\d{4}([ -])\d{4}\1\d{4}\1\d{4}(?![\d])")


def _pan(text: str) -> Iterator[PiiMatch]:
    for pattern in (_PAN_UPPER, _PAN_ANY_CASE, _PAN_GLUED):
        for m in pattern.finditer(text):
            yield PiiMatch(m.start(), m.end(), PiiType.PAN)
    for m in _PAN_AFTER_KEYWORD.finditer(text):
        yield PiiMatch(m.start(1), m.end(1), PiiType.PAN)


def _aadhaar(text: str) -> Iterator[PiiMatch]:
    for m in _AADHAAR.finditer(text):
        digits = m.group(1) + m.group(3) + m.group(4)
        window = text[max(0, m.start() - 40) : m.start()]
        if verhoeff.is_valid(digits) or _AADHAAR_KEYWORD.search(window):
            yield PiiMatch(m.start(), m.end(), PiiType.AADHAAR)


def _phone(text: str) -> Iterator[PiiMatch]:
    for m in _PHONE.finditer(text):
        yield PiiMatch(m.start(), m.end(), PiiType.PHONE)


def _email(text: str) -> Iterator[PiiMatch]:
    for m in _EMAIL.finditer(text):
        yield PiiMatch(m.start(), m.end(), PiiType.EMAIL)
    for m in _UPI.finditer(text):
        yield PiiMatch(m.start(), m.end(), PiiType.UPI)


def _account(text: str) -> Iterator[PiiMatch]:
    for m in _ACCOUNT.finditer(text):
        yield PiiMatch(m.start(1), m.end(1), PiiType.ACCOUNT)
    for m in _DEMAT_NSDL.finditer(text):
        yield PiiMatch(m.start(), m.end(), PiiType.ACCOUNT)


def _otp(text: str) -> Iterator[PiiMatch]:
    for pattern in (_OTP_AFTER, _OTP_BEFORE):
        for m in pattern.finditer(text):
            if not _YEAR.fullmatch(m.group(1)):
                yield PiiMatch(m.start(1), m.end(1), PiiType.OTP)


def _long_numbers(text: str) -> Iterator[PiiMatch]:
    for pattern in (_LONG_NUMBER, _CARD):
        for m in pattern.finditer(text):
            yield PiiMatch(m.start(), m.end(), PiiType.ID_NUMBER)


STRUCTURED_DETECTORS: tuple[Callable[[str], Iterator[PiiMatch]], ...] = (
    _pan,
    _aadhaar,
    _phone,
    _email,
    _account,
    _otp,
    _long_numbers,
)


def detect_structured(text: str) -> list[PiiMatch]:
    return [match for detector in STRUCTURED_DETECTORS for match in detector(text)]
