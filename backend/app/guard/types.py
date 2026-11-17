from dataclasses import dataclass
from enum import StrEnum


class PiiType(StrEnum):
    PAN = "pan"
    AADHAAR = "aadhaar"
    PHONE = "phone"
    EMAIL = "email"
    UPI = "upi"
    ACCOUNT = "account"
    OTP = "otp"
    ID_NUMBER = "id_number"  # long unexplained digit strings: card numbers, ids
    NAME = "name"

    @property
    def token(self) -> str:
        return f"[{self.value.upper()}_REDACTED]"


# Higher wins when two detections cover exactly the same length of text.
PRIORITY: dict[PiiType, int] = {
    PiiType.PAN: 90,
    PiiType.AADHAAR: 85,
    PiiType.UPI: 80,
    PiiType.EMAIL: 80,
    PiiType.OTP: 70,
    PiiType.ACCOUNT: 65,
    PiiType.PHONE: 60,
    PiiType.ID_NUMBER: 40,
    PiiType.NAME: 30,
}


@dataclass(frozen=True)
class PiiMatch:
    """A detected span. Callers get the type and position, never a copy of the value."""

    start: int
    end: int
    type: PiiType

    @property
    def length(self) -> int:
        return self.end - self.start
