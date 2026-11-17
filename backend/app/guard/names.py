"""Person-name detection for incoming text only.

Names are redacted from customer messages, but never used to block outgoing drafts:
corpus facts can legitimately name a fund manager.
Uses spaCy's small English model directly (architecture section 4 lists Presidio, which wraps
the same model but adds dependencies, one of which downloads data on first use).
"""

import re
from collections.abc import Iterator
from functools import lru_cache
from typing import Any

from app.guard.types import PiiMatch, PiiType

# Words that spaCy sometimes tags as people but are products, regulators or finance terms.
_DOMAIN_WORDS = frozenset(
    """
    hdfc sebi amfi indmoney ind money groww kuvera powerup wealth monitor elss sip nav kyc
    nifty sensex bse nse mutual fund funds liquid flexi cap mid large small tax saver direct
    regular growth idcw plan cams kfintech kfin karvy crisil icra upi aadhaar aadhar pan demat
    isin ter stt gst tds rta amc sid kim riskometer benchmark tri exit load lock-in redemption
    """.split()
)

_STOP_WORDS = frozenset(
    "and or but my i is am from here with the a an to for of in on at please pls thanks "
    "thank you sir madam mam calling regarding about".split()
)
_NAME_INTRO = re.compile(r"(?i)\b(?:my\s+name\s+is|name\s*[:=-]|this\s+is|i\s+am|i'm)\s+")
_WORD = re.compile(r"[A-Za-z][A-Za-z'.-]*")


@lru_cache(maxsize=1)
def _nlp() -> Any:
    import spacy

    return spacy.load(
        "en_core_web_sm",
        exclude=["parser", "lemmatizer", "attribute_ruler", "tagger", "senter"],
    )


def _is_domain_term(text: str) -> bool:
    return any(word.lower() in _DOMAIN_WORDS for word in _WORD.findall(text))


def _intro_names(text: str) -> Iterator[PiiMatch]:
    """'my name is priya sharma' -> span over 'priya sharma'. Catches lower-case names."""
    for intro in _NAME_INTRO.finditer(text):
        is_explicit = "name" in intro.group(0).lower()
        pos = intro.end()
        start = end = pos
        count = 0
        for word in _WORD.finditer(text, pos):
            gap = text[end : word.start()]
            if count and (gap.strip() or "\n" in gap):
                break
            if not count and word.start() != pos:
                break
            token = word.group(0)
            if token.lower() in _STOP_WORDS or _is_domain_term(token):
                break
            # "I am confused": only take capitalised words unless the intro says "name".
            if not is_explicit and not token[0].isupper():
                break
            end = word.end()
            count += 1
            if count == 3:
                break
        if count:
            yield PiiMatch(start, end, PiiType.NAME)


def detect_names(text: str) -> list[PiiMatch]:
    matches = list(_intro_names(text))
    for ent in _nlp()(text).ents:
        if (
            ent.label_ == "PERSON"
            and not _is_domain_term(ent.text)
            and "REDACTED" not in ent.text
            and not any(ch.isdigit() for ch in ent.text)  # names never contain digits
        ):
            matches.append(PiiMatch(ent.start_char, ent.end_char, PiiType.NAME))
    return matches
