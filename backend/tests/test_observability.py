import pytest

from app import observability


@pytest.mark.allow_pii_logs
def test_custom_redactor_applies_to_every_string_value() -> None:
    previous = observability.get_pii_redactor()
    observability.set_pii_redactor(lambda s: s.replace("ABCDE1234F", "[PAN_REDACTED]"))
    try:
        event = {"event": "x", "note": "PAN ABCDE1234F", "count": 3}
        out = observability._redact_values(None, "info", event)
        assert out["note"] == "PAN [PAN_REDACTED]"
        assert out["count"] == 3
    finally:
        observability.set_pii_redactor(previous)
