from app import observability


def test_redactor_applies_to_every_string_value() -> None:
    observability.set_pii_redactor(lambda s: s.replace("ABCDE1234F", "[PAN_REDACTED]"))
    try:
        event = {"event": "x", "note": "PAN ABCDE1234F", "count": 3}
        out = observability._redact_values(None, "info", event)
        assert out["note"] == "PAN [PAN_REDACTED]"
        assert out["count"] == 3
    finally:
        observability.set_pii_redactor(observability._identity)
