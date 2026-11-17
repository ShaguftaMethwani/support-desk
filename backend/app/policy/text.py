"""Counting helpers for the length caps: sentences, words, bullets, and URLs.

Sentence splitting must not break on abbreviations ("Rs.", "e.g.", "approx."), decimals
("1.5%"), URLs or emails, all of which are common in mutual-fund answers.
"""

import re
from urllib.parse import urlsplit

# Never end a sentence after these.
_ABBREVIATIONS = (
    "rs", "re", "e.g", "i.e", "approx", "vs", "viz", "mr", "mrs", "ms", "dr", "sr", "jr",
    "no", "nos", "ref", "amt", "avg", "min", "max", "yr", "yrs", "hrs", "p.a", "a.m", "p.m",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
    "fig", "sec", "cl", "u.s", "govt", "dept",
)  # fmt: skip
# These often end a sentence ("... HDFC Mutual Fund Ltd. It is ..."), so they only stay
# joined when the next word starts in lower case or with a digit.
_SOFT_ABBREVIATIONS = ("ltd", "pvt", "inc", "co", "corp", "etc", "st")
_PLACEHOLDER = "․"  # one-dot leader, stands in for protected dots

_URL = re.compile(r"\bhttps?://[^\s<>\"')\]]+", re.IGNORECASE)
_EMAIL_LIKE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_DECIMAL = re.compile(r"(?<=\d)\.(?=\d)")
_ABBREV = re.compile(
    r"(?<![\w.])("
    + "|".join(re.escape(a) for a in sorted(_ABBREVIATIONS, key=len, reverse=True))
    + r")\.(?=\s|$|[,;:)])",
    re.IGNORECASE,
)
_SOFT_ABBREV = re.compile(
    r"(?<![\w.])((?i:" + "|".join(_SOFT_ABBREVIATIONS) + r"))\.(?=\s+[a-z0-9]|[,;:)])"
)
_INITIAL = re.compile(r"(?<![\w.])([A-Z])\.(?=\s*[A-Z]\.)")
# A boundary is end punctuation followed by a word that can start a sentence.
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"')\]]*\s+(?=[A-Z0-9\"'(\[₹])")
_ELLIPSIS = re.compile(r"\.{2,}|…")
_LAST_CHECKED = re.compile(r"^\s*last\s+checked\s*:", re.IGNORECASE)
_SOURCE_LINE = re.compile(r"^\s*(?:source|sources|link|citation)?\s*:?\s*https?://\S+\s*$", re.I)
_BULLET = re.compile(r"^\s*(?:[-*•‣▪◦]|\d{1,2}[.)])\s+\S")
_WORD_TOKEN = re.compile(r"[A-Za-z0-9₹%]+(?:[.'’,/-][A-Za-z0-9%]+)*")


def _protect(text: str) -> str:
    text = _URL.sub(lambda m: m.group(0).replace(".", _PLACEHOLDER), text)
    text = _EMAIL_LIKE.sub(lambda m: m.group(0).replace(".", _PLACEHOLDER), text)
    text = _DECIMAL.sub(_PLACEHOLDER, text)
    text = _ELLIPSIS.sub(lambda m: _PLACEHOLDER * len(m.group(0)), text)
    text = _ABBREV.sub(lambda m: m.group(1).replace(".", _PLACEHOLDER) + _PLACEHOLDER, text)
    text = _SOFT_ABBREV.sub(lambda m: m.group(1) + _PLACEHOLDER, text)
    return _INITIAL.sub(lambda m: m.group(1) + _PLACEHOLDER, text)


def content_lines(text: str) -> list[str]:
    """Lines of a reply that count toward its length: no 'Last checked' or bare source lines."""
    return [
        line
        for line in text.splitlines()
        if line.strip() and not _LAST_CHECKED.match(line) and not _SOURCE_LINE.match(line)
    ]


def split_sentences(text: str) -> list[str]:
    sentences: list[str] = []
    for line in content_lines(text):
        protected = _protect(line)
        for part in _SENTENCE_END.split(protected):
            if part and part.strip():
                sentences.append(part.replace(_PLACEHOLDER, ".").strip())
    return sentences


def count_sentences(text: str) -> int:
    return len(split_sentences(text))


def count_words(text: str) -> int:
    return len(_WORD_TOKEN.findall(text))


def count_bullets(text: str) -> int:
    return sum(1 for line in text.splitlines() if _BULLET.match(line))


def find_urls(text: str) -> list[str]:
    return [m.group(0).rstrip(".,;:") for m in _URL.finditer(text)]


def domain_allowed(url: str, allowed: list[str]) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return False
    return any(host == d or host.endswith("." + d) for d in allowed)
