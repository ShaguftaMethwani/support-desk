"""The ingress guard: normalise, detect, redact.

guard() is the single entry point for every piece of text entering the system: typed
messages, voice transcripts, CSV rows, draft edits and refinement feedback. It returns
clean text and the list of PII types found. The original values are never returned.
"""

from dataclasses import dataclass

from app.guard.detectors import detect_structured
from app.guard.names import detect_names
from app.guard.spoken import normalise_spoken
from app.guard.types import PRIORITY, PiiMatch, PiiType


@dataclass(frozen=True)
class GuardResult:
    text: str
    pii_flags: list[str]
    redaction_count: int

    @property
    def had_pii(self) -> bool:
        return self.redaction_count > 0


def _resolve(matches: list[PiiMatch]) -> list[PiiMatch]:
    """Drop overlaps. Longer spans win, then higher-priority types."""
    ranked = sorted(matches, key=lambda m: (-m.length, -PRIORITY[m.type], m.start))
    kept: list[PiiMatch] = []
    for match in ranked:
        if all(match.end <= k.start or match.start >= k.end for k in kept):
            kept.append(match)
    return sorted(kept, key=lambda m: m.start)


def find_pii(text: str, *, names: bool = False, normalise: bool = True) -> list[PiiType]:
    """Which PII types appear in `text`. Used by validators: reports types, never values."""
    checked = normalise_spoken(text) if normalise else text
    matches = detect_structured(checked)
    if names:
        matches += detect_names(checked)
    return sorted({m.type for m in _resolve(matches)})


def _redact(text: str, matches: list[PiiMatch]) -> str:
    out = text
    for match in reversed(_resolve(matches)):
        out = out[: match.start] + match.type.token + out[match.end :]
    return out


def guard(text: str, *, names: bool = True) -> GuardResult:
    """Normalise spoken numbers, then redact every PII match with a typed token."""
    normalised = normalise_spoken(text)
    matches = detect_structured(normalised)
    if names:
        matches += detect_names(normalised)
    resolved = _resolve(matches)
    flags = sorted({m.type.value for m in resolved})
    return GuardResult(_redact(normalised, resolved), flags, len(resolved))


def redact_structured(text: str) -> tuple[str, int]:
    """Fast redaction without names or normalisation, for log lines."""
    matches = _resolve(detect_structured(text))
    return _redact(text, matches), len(matches)
