import pytest
from structlog.testing import capture_logs

from app.guard.log_filter import PII_LOG_COUNTER, redact_log_value
from app.guard.redactor import redact_structured
from app.observability import _redact_values, counter_value, get_pii_redactor


@pytest.mark.allow_pii_logs
def test_log_values_are_redacted_and_counted() -> None:
    before = counter_value(PII_LOG_COUNTER)
    assert redact_log_value("user typed 9876543210") == "user typed [PHONE_REDACTED]"
    assert counter_value(PII_LOG_COUNTER) == before + 1


@pytest.mark.allow_pii_logs
def test_installed_redactor_runs_on_every_string_field() -> None:
    event = {"event": "x", "note": "PAN ABCPD1234F", "count": 3}
    out = _redact_values(None, "info", event)
    assert out["note"] == "PAN [PAN_REDACTED]"
    assert out["count"] == 3


def test_clean_logs_do_not_touch_the_counter() -> None:
    before = counter_value(PII_LOG_COUNTER)
    redact_log_value("llm_call task=facts model=x tokens=42")
    assert counter_value(PII_LOG_COUNTER) == before


def test_redactor_is_installed_for_the_test_session() -> None:
    assert get_pii_redactor() is redact_log_value
    with capture_logs():
        pass


@pytest.mark.allow_pii_logs  # the last line redacts a phone number on purpose
def test_ids_are_never_read_as_pii() -> None:
    # Random hex ids can hold 12 digits in a row, which read like an Aadhaar number. This one
    # is shaped like one from a real test run, logged as "280b[AADHAAR_REDACTED]b0a1...".
    rid = "280b234567890107b0a1b67659c41466"
    assert redact_structured(rid)[1] == 1  # the detector alone flags it
    before = counter_value(PII_LOG_COUNTER)
    event = {"event": "request", "request_id": rid,
             "item_id": "280b2345-6789-0107-b0a1-b67659c41466"}  # fmt: skip
    out = _redact_values(None, "info", dict(event))
    assert out == event
    assert counter_value(PII_LOG_COUNTER) == before
    # A value that only contains an id among other text is still checked.
    assert "REDACTED" in _redact_values(None, "info", {"note": f"{rid} 9876543210"})["note"]
