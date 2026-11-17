"""Redact PII from every log line before it is written (architecture section 6.3)."""

from app.guard.redactor import redact_structured
from app.observability import increment, set_pii_redactor

PII_LOG_COUNTER = "pii_log_redactions"


def redact_log_value(text: str) -> str:
    clean, count = redact_structured(text)
    if count:
        increment(PII_LOG_COUNTER, count)
    return clean


def install_log_redaction() -> None:
    set_pii_redactor(redact_log_value)
