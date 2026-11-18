"""The five HDFC schemes in scope, with the names they appear under.

Names were confirmed against AMFI's scheme master data (portal.amfiindia.com, November 2026).
Older and alternative names are kept as aliases so that documents and user questions using
them still map to the right scheme.
"""

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Scheme:
    slug: str
    name: str
    category: str
    aliases: tuple[str, ...]


SCHEMES: tuple[Scheme, ...] = (
    Scheme(
        "hdfc_large_cap",
        "HDFC Large Cap Fund",
        "Large Cap",
        ("HDFC Large Cap Fund", "HDFC Top 100 Fund", "HDFC Top 100", "large cap fund", "large cap"),
    ),
    Scheme(
        "hdfc_flexi_cap",
        "HDFC Flexi Cap Fund",
        "Flexi Cap",
        ("HDFC Flexi Cap Fund", "HDFC Equity Fund", "flexi cap fund", "flexicap", "flexi cap"),
    ),
    Scheme(
        "hdfc_elss_tax_saver",
        "HDFC ELSS Tax Saver",
        "ELSS",
        (
            "HDFC ELSS Tax Saver",
            "HDFC ELSS Tax saver",
            "HDFC ELSS - Tax Saver Fund",
            "HDFC ELSS Tax Saver Fund",
            "HDFC TaxSaver",
            "HDFC Tax Saver",
            "tax saver fund",
            "tax saver",
            "elss fund",
            "elss",
        ),
    ),
    Scheme(
        "hdfc_mid_cap",
        "HDFC Mid Cap Fund",
        "Mid Cap",
        (
            "HDFC Mid Cap Fund",
            "HDFC Mid-Cap Opportunities Fund",
            "HDFC Mid Cap Opportunities Fund",
            "HDFC Midcap Fund",
            "mid cap fund",
            "midcap fund",
            "mid-cap",
            "mid cap",
            "midcap",
        ),
    ),
    Scheme(
        "hdfc_liquid",
        "HDFC Liquid Fund",
        "Liquid",
        ("HDFC Liquid Fund", "liquid fund", "liquid"),
    ),
)

BY_SLUG = {s.slug: s for s in SCHEMES}
GENERAL = "general"


def _alias_pattern(alias: str) -> str:
    # Hyphens and spaces are interchangeable: "Mid-Cap" == "Mid Cap".
    parts = re.split(r"[\s-]+", alias.strip())
    return r"\b" + r"[\s-]*".join(re.escape(p) for p in parts) + r"\b"


# Longest aliases first, so "HDFC Mid Cap Fund" wins over "mid cap".
_ALIASES = sorted(
    ((alias, scheme.slug) for scheme in SCHEMES for alias in scheme.aliases),
    key=lambda pair: -len(pair[0]),
)
_ALIAS_RE = re.compile(
    "|".join(f"(?P<a{i}>{_alias_pattern(a)})" for i, (a, _) in enumerate(_ALIASES)), re.IGNORECASE
)
_SLUG_BY_GROUP = {f"a{i}": slug for i, (_, slug) in enumerate(_ALIASES)}

# Any HDFC scheme name, used to recognise pages about schemes that are out of scope.
_ANY_HDFC_SCHEME = re.compile(
    r"\bHDFC\s+(?:(?:[A-Z0-9][\w&.'-]*|[-–&]|and|of)\s+){0,7}?(?i:fund|plan|etf|fof|saver|scheme)\b"
)


def find_schemes(text: str) -> list[str]:
    """Slugs of in-scope schemes mentioned in `text`, in order of first mention, no repeats."""
    found: list[str] = []
    for m in _ALIAS_RE.finditer(text):
        slug = _SLUG_BY_GROUP[m.lastgroup or ""]
        if slug not in found:
            found.append(slug)
    return found


def first_hdfc_scheme(text: str) -> tuple[str | None, bool]:
    """The first HDFC scheme named in `text`.

    Returns (slug, True) for an in-scope scheme, (None, True) for an out-of-scope HDFC scheme,
    and (None, False) when no HDFC scheme is named.
    """
    m = _ANY_HDFC_SCHEME.search(text)
    if m is None:
        return None, False
    # The whole HDFC name must be one of ours: "HDFC NIFTY Midcap 150 Index Fund" is not
    # "HDFC Mid Cap Fund", even though it contains "Midcap".
    name = _norm(m.group(0))
    return _FULL_NAMES.get(name), True


def _norm(name: str) -> str:
    return re.sub(r"[\s-]+", " ", name).strip().lower()


_FULL_NAMES = {
    _norm(alias): scheme.slug
    for scheme in SCHEMES
    for alias in scheme.aliases
    if alias.upper().startswith("HDFC")
}
