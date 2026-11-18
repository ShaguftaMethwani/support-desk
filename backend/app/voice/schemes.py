"""Misheard scheme names: suggestions for the user, never silent fixes.

Whisper sometimes hears "mid cap" as "big cap". Two- and three-word stretches of the
transcript are compared with each scheme's short name using rapidfuzz. A close match that is
not already a correct scheme name becomes a suggestion, with its position in the transcript,
for the user to accept or ignore at the confirmation step.

Single words are not compared: "else" scores 75 against "ELSS". Neither are stretches that
already name a scheme, or that say "small" (HDFC Small Cap Fund exists but is out of scope,
so "small cap" must not become "mid cap").
"""

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from app.corpus.schemes import BY_SLUG, find_schemes

# The short names people say, per scheme.
SPOKEN_NAMES: dict[str, tuple[str, ...]] = {
    "hdfc_large_cap": ("Large Cap",),
    "hdfc_flexi_cap": ("Flexi Cap",),
    "hdfc_elss_tax_saver": ("ELSS Tax Saver", "Tax Saver"),
    "hdfc_mid_cap": ("Mid Cap",),
    "hdfc_liquid": ("Liquid Fund",),
}
_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_SKIP = re.compile(r"\bsmall\b", re.IGNORECASE)


@dataclass(frozen=True)
class SchemeSuggestion:
    heard: str
    start: int  # character offsets in the clean transcript
    end: int
    suggestion: str  # replacement text, e.g. "Mid Cap"
    scheme: str  # slug
    scheme_name: str
    score: float

    def as_dict(self) -> dict[str, object]:
        return {
            "heard": self.heard, "start": self.start, "end": self.end,
            "suggestion": self.suggestion, "scheme": self.scheme,
            "scheme_name": self.scheme_name, "score": round(self.score, 1),
        }  # fmt: skip


def suggest_schemes(text: str, *, min_score: float) -> list[SchemeSuggestion]:
    named = set(find_schemes(text))
    words = list(_WORD.finditer(text))
    candidates: list[SchemeSuggestion] = []
    for size in (2, 3):
        for i in range(len(words) - size + 1):
            start, end = words[i].start(), words[i + size - 1].end()
            heard = text[start:end]
            if find_schemes(heard) or _SKIP.search(heard):
                continue
            for slug, names in SPOKEN_NAMES.items():
                if slug in named:
                    continue
                for name in names:
                    score = fuzz.ratio(heard.lower(), name.lower())
                    if score >= min_score:
                        candidates.append(SchemeSuggestion(
                            heard, start, end, name, slug, BY_SLUG[slug].name, score
                        ))  # fmt: skip
    # Best first; overlapping stretches keep only the best one.
    kept: list[SchemeSuggestion] = []
    for c in sorted(candidates, key=lambda c: (-c.score, c.start)):
        if all(c.end <= k.start or c.start >= k.end for k in kept):
            kept.append(c)
    return sorted(kept, key=lambda c: c.start)
