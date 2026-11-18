"""Refinement feedback (architecture section 7.7): which agents a piece of feedback re-runs.

| Feedback type      | Re-runs               | Reuses                          |
| length_tone        | orchestrator, reviewer | Facts, Pattern, Compliance     |
| different_scheme   | Facts, orch., reviewer | Pattern, Compliance            |
| past_theme         | Pattern, orch., rev.   | Facts, Compliance              |
| other              | orchestrator, reviewer | everything (the fallback)      |

Keyword rules decide first. Free text they can't place goes to the small model; if that fails
or answers something else, the deterministic fallback is "other" (orchestrator only).
"""

import re
from collections.abc import Mapping, Sequence
from typing import Protocol

from pydantic import BaseModel

from app.intent.rules import find_scheme
from app.llm import LLMError, LLMResult

FEEDBACK_TYPES = ("length_tone", "different_scheme", "past_theme", "other")
AGENTS_FOR: dict[str, tuple[str, ...]] = {
    "length_tone": (),
    "different_scheme": ("facts",),
    "past_theme": ("pattern",),
    "other": (),
}

_I = re.IGNORECASE
_LENGTH_TONE = re.compile(
    r"\b(?:shorter|longer|short|brief|briefer|concise|simpler|simple|plain(?:er)?|tone|"
    r"friendlier|friendly|warmer|polite|formal|less\s+formal|casual|rephrase|reword|wordy|"
    r"too\s+long|one\s+sentence|two\s+sentences)\b",
    _I,
)
_SCHEME = re.compile(
    r"\b(?:wrong|different|other|another)\s+(?:scheme|fund)\b|\bnot\s+(?:the\s+)?(?:\w+\s+){0,3}"
    r"(?:scheme|fund)\b|\b(?:they|customer)\s+(?:meant|means|asked\s+about)\b",
    _I,
)
_THEME = re.compile(
    r"\b(?:theme|pattern|trend|recurring|last\s+week|this\s+week|previous\s+week|fee\s+issue|"
    r"same\s+issue|other\s+users|many\s+users|similar\s+(?:complaints?|questions?|issues?)|"
    r"spike|rising|pulse)\b",
    _I,
)


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


FEEDBACK_PROMPT = """A support agent gave feedback on a drafted reply. Classify it.

- length_tone: change the length, tone or wording only
- different_scheme: the answer is about the wrong scheme or fund
- past_theme: check whether this matches a known issue, trend or past theme
- other: anything else

Reply with JSON only: {"type": "<label>"}"""


class _FeedbackOut(BaseModel):
    type: str


def feedback_by_rules(text: str) -> str | None:
    if _THEME.search(text):
        return "past_theme"
    if _SCHEME.search(text) or find_scheme(text):
        return "different_scheme"
    if _LENGTH_TONE.search(text):
        return "length_tone"
    return None


async def classify_feedback(llm: Completer, text: str) -> str:
    kind = feedback_by_rules(text)
    if kind:
        return kind
    try:
        out = await llm.complete(
            "refine_classify",
            [{"role": "system", "content": FEEDBACK_PROMPT}, {"role": "user", "content": text}],
            schema=_FeedbackOut,
            max_tokens=100,
        )
    except LLMError:
        return "other"
    parsed = out.parsed
    label = parsed.type.strip().lower() if isinstance(parsed, _FeedbackOut) else "other"
    return label if label in FEEDBACK_TYPES else "other"
