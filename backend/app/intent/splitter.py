"""Question splitter (architecture section 7.2).

1. Code rules: split on list items, line breaks, question marks, and joining words ("and",
   "also", "plus", commas, semicolons) when both sides read as a question (they contain a
   question word or verb). Most messages are settled here with no model call.
2. Model only when unsure: a joining word between two fact topics with no question word on
   the right ("exit load and expense ratio for liquid fund") could be one question or two.
3. Coverage check: every part must be built from the message's own words, and together the
   parts must cover its content. Otherwise the message stays one part. The model can split
   the text but cannot add to it.
4. Shared slots: a scheme or fee type named once applies to every part that lacks one, taken
   from the nearest part that names one. Pronouns ("it", "that one") resolve the same way.
5. Duplicates are merged, and parts beyond the fifth are dropped and reported.
"""

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.intent.rules import find_fee_type, find_scheme, names_other_scheme
from app.llm import LLMError, LLMResult

MAX_PARTS = 5
COVERAGE_MIN = 0.8
SplitMethod = Literal["rules", "model", "single"]

_QUESTION_WORDS = (
    "what", "whats", "what's", "how", "when", "where", "which", "who", "why", "whom", "whose",
    "is", "are", "was", "were", "can", "could", "do", "does", "did", "will", "would", "should",
    "shall", "may", "might", "must", "has", "have", "tell", "explain", "show", "give", "list",
    "share", "need", "want", "let", "help", "please", "any",
)  # fmt: skip
_QUESTION_WORD = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in _QUESTION_WORDS) + r")\b",
                            re.IGNORECASE)  # fmt: skip
_LIST_ITEM = re.compile(r"^\s*(?:[-*•‣▪◦]|\(?\d{1,2}[.)]|\(?[a-e][.)])\s+")
_AFTER_QUESTION_MARK = re.compile(r"(?<=\?)\s+")
_JOINER = re.compile(
    r"\s*[,;]\s*(?:and\s+also\s+|and\s+|also\s+|plus\s+)?|\s+(?:and\s+also|and|also|plus)\s+",
    re.IGNORECASE,
)
# Fact topics. "A and B" with a topic on each side and no question word on the right is the
# unsure case the model settles.
TOPICS = re.compile(
    r"\b(?:exit\s+load|expense\s+ratio|ter|lock[\s-]?in|minimum\s+(?:sip|investment|amount)|"
    r"min(?:imum)?\s+sip|benchmark|riskometer|risk\s+level|nav|aum|fund\s+manager|stamp\s+duty|"
    r"stt|tax(?:ation)?|sip|lumpsum|lump\s+sum|statement|capital\s+gains?|redemption|"
    r"cut[\s-]?off|plans?|options?|idcw|dividend|category|portfolio|holdings?)\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_STOP = frozenset(
    "a an the of for on in to at by with and or also plus is are was were be been do does did "
    "what whats what's how when where which who why can could would will should shall may might "
    "i me my we our you your it its this that these those there please tell about any some "
    "s me give know want need let".split()
)


@dataclass(frozen=True)
class Part:
    text: str
    scheme: str | None = None
    fee_type: str | None = None
    scheme_inherited: bool = False
    fee_inherited: bool = False

    @property
    def slots(self) -> dict[str, str | bool | None]:
        return {
            "scheme": self.scheme,
            "fee_type": self.fee_type,
            "scheme_inherited": self.scheme_inherited,
            "fee_inherited": self.fee_inherited,
        }


@dataclass(frozen=True)
class SplitResult:
    parts: list[Part]
    method: SplitMethod
    dropped: list[str] = field(default_factory=list)
    merged_duplicates: int = 0


class Completer(Protocol):
    async def complete(
        self,
        task: str,
        messages: Sequence[Mapping[str, str]],
        *,
        schema: type[BaseModel] | None = None,
        json_mode: bool = False,
        temperature: float = 0.0,
        max_tokens: int = 512,
    ) -> LLMResult: ...


# --- rules ----------------------------------------------------------------------------------


_LEAD = re.compile(r"^\s*(?:(?:and|also|so|then|plus|but|or|please)\s+)*", re.IGNORECASE)


def _starts_like_question(text: str) -> bool:
    """Starts with a question word or verb ("what", "how", "is", "can", "tell", ...), after
    any joining word. A verb in the middle doesn't count: "support is not replying" is not a
    question, and neither is "a lumpsum in the same folio?" (the end of a longer question)."""
    words = _WORD.findall(_LEAD.sub("", text.lower()))
    return bool(words) and words[0] in _QUESTION_WORDS


def _is_question(text: str) -> bool:
    return _starts_like_question(text)


def _clean(text: str) -> str:
    text = _LIST_ITEM.sub("", text).strip()
    return text.strip(" ,;").strip()


def _split_joiners(piece: str) -> tuple[list[str], bool]:
    """Split one sentence on joining words where both sides are questions.
    Returns (parts, unsure). Parts are exact slices of `piece`."""
    spans: list[tuple[int, int]] = []
    last = 0
    for m in _JOINER.finditer(piece):
        spans.append((last, m.start()))
        last = m.end()
    spans.append((last, len(piece)))
    spans = [(a, b) for a, b in spans if piece[a:b].strip()]
    if len(spans) <= 1:
        return [piece], False
    out = [spans[0]]
    unsure = False
    for a, b in spans[1:]:
        left, right = piece[out[-1][0] : out[-1][1]], piece[a:b]
        if _is_question(right) and _is_question(left):
            out.append((a, b))
            continue
        # Not a question on its own: it stays with the previous part.
        if TOPICS.search(left) and TOPICS.search(right) and not _QUESTION_WORD.search(right):
            unsure = True  # "exit load and expense ratio": one question or two?
        out[-1] = (out[-1][0], b)
    return [piece[a:b] for a, b in out], unsure


def split_by_rules(text: str) -> tuple[list[str], bool]:
    """Parts found by the code rules, and whether the rules are unsure."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    segments = [_clean(ln) for ln in lines] if len(lines) > 1 else [text.strip()]
    parts: list[str] = []
    unsure = False
    for segment in segments:
        for sentence in _sentences(segment):
            split, doubt = _split_joiners(sentence)
            unsure = unsure or doubt
            parts.extend(_clean(p) for p in split)
    return [p for p in parts if p], unsure


def _sentences(segment: str) -> list[str]:
    """Split after question marks. A piece that is not a question ("I mean HDFC ELSS Tax
    Saver.") stays with the question before it, as one exact slice of the segment."""
    spans: list[tuple[int, int]] = []
    last = 0
    for m in _AFTER_QUESTION_MARK.finditer(segment):
        spans.append((last, m.start()))
        last = m.end()
    spans.append((last, len(segment)))
    merged: list[tuple[int, int]] = []
    for a, b in spans:
        piece = segment[a:b]
        if not piece.strip():
            continue
        if merged and not (piece.rstrip().endswith("?") or _starts_like_question(piece)):
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return [segment[a:b].strip() for a, b in merged]


# --- model fallback and coverage --------------------------------------------------------------

SPLIT_PROMPT = """You split a customer's message into separate questions.

Rules:
- Only split when the message really asks two or more different things.
- Use only words from the message. Do not add facts, schemes or new questions.
- You may repeat a shared subject so each part makes sense on its own, e.g.
  "exit load and expense ratio of HDFC Liquid Fund" ->
  ["exit load of HDFC Liquid Fund", "expense ratio of HDFC Liquid Fund"].
- If it is one question, return it as a single part.

Reply with JSON only: {"parts": ["...", "..."]}"""


class _SplitOut(BaseModel):
    parts: list[str] = Field(default_factory=list)


def content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if w not in _STOP}


def coverage_ok(original: str, parts: list[str]) -> bool:
    """Every part uses only the original's words, and together they cover most of them.
    List markers ("1.", "-") are not content."""
    source = content_words("\n".join(_LIST_ITEM.sub("", ln) for ln in original.splitlines()))
    if not source or not parts:
        return False
    used: set[str] = set()
    for part in parts:
        words = content_words(part)
        if not words or not words <= source:
            return False
        used |= words
    return len(used & source) / len(source) >= COVERAGE_MIN


async def split_by_model(llm: Completer, text: str) -> list[str] | None:
    try:
        result = await llm.complete(
            "split",
            [{"role": "system", "content": SPLIT_PROMPT}, {"role": "user", "content": text}],
            schema=_SplitOut,
            max_tokens=400,
        )
    except LLMError:
        return None
    parsed = result.parsed
    if not isinstance(parsed, _SplitOut):
        return None
    parts = [" ".join(p.split()) for p in parsed.parts if p.strip()]
    return parts if coverage_ok(text, parts) else None


# --- slots, dedupe, cap ---------------------------------------------------------------------


def share_slots(texts: list[str]) -> list[Part]:
    """Each part keeps its own scheme and fee type; parts without one take the nearest named
    one, preferring an earlier part. Pronouns resolve the same way."""
    schemes = [find_scheme(t) for t in texts]
    fees = [find_fee_type(t) for t in texts]

    def nearest(values: list[str | None], i: int) -> str | None:
        for distance in range(1, len(values)):
            for j in (i - distance, i + distance):  # earlier part first
                if 0 <= j < len(values) and values[j] is not None:
                    return values[j]
        return None

    parts = []
    for i, text in enumerate(texts):
        scheme = schemes[i] or (None if names_other_scheme(text) else nearest(schemes, i))
        # A part with its own topic ("the ELSS lock-in") doesn't take another part's fee type.
        # Pronouns ("it") point at the scheme, not the fee.
        own_topic = bool(TOPICS.search(text))
        fee = fees[i] or (None if own_topic else nearest(fees, i))
        parts.append(Part(text, scheme, fee, schemes[i] is None and scheme is not None,
                          fees[i] is None and fee is not None))  # fmt: skip
    return parts


def _key(text: str) -> frozenset[str]:
    return frozenset(content_words(text))


def dedupe(parts: list[Part]) -> tuple[list[Part], int]:
    kept: list[Part] = []
    seen: list[tuple[frozenset[str], str | None]] = []
    for p in parts:
        key = (_key(p.text), p.scheme)
        if any(k == key[0] and s == key[1] for k, s in seen):
            continue
        seen.append(key)
        kept.append(p)
    return kept, len(parts) - len(kept)


async def split_message(text: str, llm: Completer | None = None) -> SplitResult:
    """Split a redacted message into 1 to MAX_PARTS parts with shared slots."""
    text = text.strip()
    pieces, unsure = split_by_rules(text)
    method: SplitMethod = "rules" if len(pieces) > 1 else "single"
    if unsure and llm is not None:
        candidates = await split_by_model(llm, text)
        if candidates and len(candidates) > len(pieces):
            pieces, method = candidates, "model"
    if len(pieces) > 1 and not coverage_ok(text, pieces):
        pieces, method = [text], "single"
    if not pieces:
        pieces, method = [text], "single"
    parts, merged = dedupe(share_slots(pieces))
    if len(parts) == 1 and method != "model":
        method = "single"
    return SplitResult(parts[:MAX_PARTS], method, [p.text for p in parts[MAX_PARTS:]], merged)
