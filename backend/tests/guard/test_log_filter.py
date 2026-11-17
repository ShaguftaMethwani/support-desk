import pytest
from structlog.testing import capture_logs

from app.guard.log_filter import PII_LOG_COUNTER, redact_log_value
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
