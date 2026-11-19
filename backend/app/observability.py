"""Structured logging.

Logs carry ids, task names, timings and validator results, never raw user text.
Every string value also passes through a PII redactor before it is written.
Phase 1 installs the real redactor from the ingress guard with set_pii_redactor().
"""

import logging
import re
from collections import Counter
from collections.abc import Callable, MutableMapping
from typing import Any

import structlog

Redactor = Callable[[str], str]


def _identity(text: str) -> str:
    return text


_redactor: Redactor = _identity


def set_pii_redactor(redactor: Redactor) -> None:
    """Install the function that strips PII from every log string."""
    global _redactor
    _redactor = redactor


def get_pii_redactor() -> Redactor:
    return _redactor


# Simple in-process counters. pii_log_redactions must stay at zero: a non-zero value means
# some code tried to log user text, which the test suite treats as a failure.
_counters: Counter[str] = Counter()


def increment(name: str, amount: int = 1) -> None:
    _counters[name] += amount


def counter_value(name: str) -> int:
    return _counters[name]


_UUID = re.compile(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}", re.I)


def _redact_values(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    for key, value in event_dict.items():
        # An id (request id, item id) can hold a run of 12 digits that reads like an Aadhaar
        # number. A value that is wholly a UUID can't be personal data, so it is left alone.
        if isinstance(value, str) and not _UUID.fullmatch(value):
            event_dict[key] = _redactor(value)
    return event_dict


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            _redact_values,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(level.upper(), logging.INFO)
        ),
        # Not cached, so structlog.testing.capture_logs() works in every test.
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
