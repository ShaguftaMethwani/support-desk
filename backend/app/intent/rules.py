"""Keyword rules for incoming questions (architecture section 7.3). Code only.

- Advice and returns pre-rules force the refusal intents. The model can't override them, so
  rephrasing can't talk the system out of a refusal.
- Soft advice signals are weaker phrasings ("is it a smart move", "worth it"). On their own
  they only make the Compliance Agent ask the model; after an advice refusal in the same
  session they count as advice (the sticky refusal).
- Slots: scheme and fee type mentioned in a question, found by code.

The outgoing validators in app/policy check what the *system* says. These rules check what
the *user* asks, which is phrased very differently ("should I", not "you should").
"""

import re

from app.corpus.schemes import find_schemes
from app.weekly.fees import FEE_TYPES

_I = re.IGNORECASE

ADVICE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (rule_id, re.compile(pattern, _I))
    for rule_id, pattern in (
        ("should_i", r"\b(?:should|shall|must)\s+i\b"),
        ("should_we", r"\bshould\s+(?:we|my\s+(?:wife|husband|family|parents?))\b"),
        ("is_it_good", r"\bis\s+(?:it|this|that|now|[\w\s-]{0,40}?fund)\s+(?:a\s+)?"
                       r"(?:good|bad|safe|right|wise|smart|worth|better|ideal)\b"),
        ("which_is_best", r"\bwhich\s+(?:\w+\s+){0,3}(?:fund|scheme|one|plan|option)s?\b[^?]{0,60}"
                          r"\b(?:best|better|choose|pick|invest|buy|go\s+for|prefer|safer)\b"),
        ("best_fund", r"\b(?:best|better|safest|top)\s+(?:mutual\s+)?(?:fund|scheme|option|plan|"
                      r"investment|choice)s?\b"),
        ("recommend", r"\b(?:recommend|suggest)\b"),
        # "advise" alone is also used for process help ("can you advise how to update KYC").
        ("advice", r"\b(?:your|any|some|need)\s+advice\b|\badvice\s+on\b|"
                   r"\badvise\s+(?:me\s+)?(?:on|whether|which|if)\b"),
        ("worth_investing", r"\bworth\s+(?:investing|it|buying|holding|the\s+risk)\b"),
        # "best time to submit a redemption request" is a cut-off question, not timing advice.
        ("good_time", r"\b(?:good|right|best|bad)\s+time\s+to\s+(?:invest|buy|sell|enter|exit|"
                      r"start|switch|book|put|redeem\s+(?:my|now|all))\b"),
        ("act_now", r"\b(?:buy|sell|redeem|invest|switch|exit|hold|withdraw)\s+(?:now|today|"
                    r"right\s+now|immediately)\b"),
        ("where_to_invest", r"\bwhere\s+(?:should|can|do)\s+i\s+(?:invest|put)\b"),
        ("better_than", r"\b(?:better|safer)\s+than\b"),
    )
)  # fmt: skip

RETURNS_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (rule_id, re.compile(pattern, _I))
    for rule_id, pattern in (
        ("how_much_return", r"\b(?:how\s+much|what)\s+(?:\w+\s+){0,3}(?:returns?|profit|gain)s?\b"),
        ("return_terms", r"\b(?:cagr|xirr|annuali[sz]ed\s+returns?|absolute\s+returns?)\b"),
        # Not "income tax returns for capital gains".
        ("returns_of", r"(?<!tax\s)(?<!itr\s)\breturns?\s+(?:of|on|for|from|in|over|last|past|"
                       r"since)\b"),
        ("past_returns", r"\b(?:last|past)\s+\d+\s*(?:years?|yrs?|months?)\b[^?]{0,40}"
                         r"\breturns?\b"),
        # Fund performance, not "the app performance is terrible".
        ("performance", r"\b(?:fund|scheme|nav|portfolio|its)\s+performance\b|"
                        r"\bperformance\s+of\s+(?!the\s+app)|"
                        r"\bhow\s+(?:has|did|is|was)\s+[^?]{0,50}?\bperform(?:ed|ing)?\b|"
                        r"\boutperform\w*|\bbeat\s+the\s+(?:market|index|benchmark)\b"),
        ("will_i_earn", r"\bhow\s+much\s+(?:will|can|would)\s+i\s+(?:get|earn|make)\b"),
        ("double_money", r"\b(?:double|triple)\s+(?:my\s+)?money\b"),
        ("highest_returns", r"\b(?:highest|best|good|guaranteed)\s+returns?\b"),
    )
)  # fmt: skip

SOFT_ADVICE: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, _I)
    for p in (
        r"\b(?:smart|wise|sensible|safe|risky|good|bad)\s+(?:move|idea|bet|choice|decision)\b",
        r"\bworth\b",
        r"\bwhere\s+would\s+you\b",
        r"\bwhat\s+would\s+you\s+do\b",
        r"\bif\s+you\s+were\s+me\b",
        r"\bgo\s+for\b",
        r"\b(?:put|park|keep)\s+(?:my\s+)?(?:money|savings|cash|\d)",
        r"\b(?:safe|safer|risky|riskier)\b",
        r"\bbetween\s+(?:the\s+)?(?:two|these|them)\b",
    )
)


def advice_rule(text: str) -> str | None:
    """Id of the first advice pre-rule matching `text`, or None."""
    return next((rule_id for rule_id, p in ADVICE_RULES if p.search(text)), None)


def returns_rule(text: str) -> str | None:
    return next((rule_id for rule_id, p in RETURNS_RULES if p.search(text)), None)


def soft_advice(text: str) -> bool:
    return any(p.search(text) for p in SOFT_ADVICE)


# --- slots ----------------------------------------------------------------------------------

# Question-side fee words. Platform charges (brokerage etc.) are not fee slots here: the
# corpus can't answer them, and they are left to the Facts Agent's "not covered".
_MF_FEE_TYPES = tuple(f for f in FEE_TYPES if f.mutual_fund)


def find_fee_type(text: str) -> str | None:
    return next((f.key for f in _MF_FEE_TYPES if f.matches(text)), None)


def find_scheme(text: str) -> str | None:
    found = find_schemes(text)
    return found[0] if found else None


# Facts that belong to one scheme. A question about one of these that names no scheme ("and
# the lock-in?") is a follow-up to the session's last scheme. Broader words like "portfolio"
# or "statement" are not: "my portfolio won't load" is about the app, not a scheme.
SCHEME_FACT = re.compile(
    r"\b(?:exit\s+load|expense\s+ratio|ter|lock[\s-]?in|minimum\s+(?:sip|investment|amount)|"
    r"min(?:imum)?\s+sip|benchmark|riskometer|risk\s+level|nav|aum|fund\s+manager|idcw|"
    r"dividend|category)\b",
    _I,
)

PRONOUN = re.compile(
    r"\b(?:it|its|it's|that\s+one|this\s+one|that\s+fund|this\s+fund|the\s+same(?:\s+fund)?|"
    r"same\s+scheme|that\s+scheme|this\s+scheme)\b",
    _I,
)
