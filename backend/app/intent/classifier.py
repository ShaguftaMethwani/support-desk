"""Intent classifier (architecture section 7.3).

Order of decisions:
1. Advice and returns pre-rules (code). A match is final: the model is never asked.
2. Sticky refusal: after an advice refusal in this session, soft advice phrasings ("is it a
   smart move") count as advice too, and so does a model "advice" label at low confidence.
3. The small model labels the rest with one of 7 intents and a confidence. It can add a
   refusal (label advice or returns) but can never remove one found by the rules.
4. Below the confidence threshold the intent becomes `unclear`.
Slots (scheme, fee type) come from code: the part's own words, then the message's shared
slots, then the session's last scheme.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.corpus.schemes import BY_SLUG
from app.intent.rules import (
    PRONOUN,
    SCHEME_FACT,
    advice_rule,
    names_other_scheme,
    returns_rule,
    soft_advice,
)
from app.intent.splitter import Part
from app.llm import LLMResult

Intent = Literal[
    "factual", "fee_canonical", "advice", "returns", "out_of_scope", "complaint", "unclear"
]
INTENTS: tuple[str, ...] = (
    "factual", "fee_canonical", "advice", "returns", "out_of_scope", "complaint", "unclear",
)  # fmt: skip
REFUSAL_INTENTS = frozenset({"advice", "returns"})


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


@dataclass(frozen=True)
class Classification:
    intent: str
    confidence: float
    method: str  # pre_rule | sticky_refusal | model | low_confidence
    rule: str | None = None  # the pre-rule id, or the model's label before low_confidence
    scheme: str | None = None
    scheme_source: str | None = None  # part | shared | session
    fee_type: str | None = None


INTENT_PROMPT = """You classify questions sent to the support desk of an Indian investing app
that answers facts about 5 HDFC Mutual Fund schemes from official documents.

Labels:
- factual: asks for a fact about a scheme or mutual funds in general (exit load, expense ratio,
  lock-in, minimum SIP, benchmark, riskometer, plan options such as growth and IDCW, what a
  charge such as stamp duty is, KYC, statements, how a process works).
- fee_canonical: confused about a fee or charge on their own investment: money deducted, or
  paying more than they expected ("why was money cut when I redeemed", "why is my regular
  plan charging more than the direct plan").
- advice: asks what to do or which to choose (buy, sell, hold, which fund, is it good).
- returns: asks about returns, performance or how much they will earn.
- complaint: reports a problem with their account, a transaction or the service.
- out_of_scope: not about mutual funds or this app at all.
- unclear: too vague to answer without asking which scheme or what they mean.

Reply with JSON only: {"intent": "<label>", "confidence": 0.0-1.0}"""


class _IntentOut(BaseModel):
    intent: str
    confidence: float = Field(ge=0.0, le=1.0)


def is_follow_up(text: str) -> bool:
    """Refers back to an earlier scheme: a pronoun ("its exit load") or a scheme fact with no
    scheme named ("and the lock-in?"). An unrelated complaint ("the app keeps crashing") is
    not a follow-up and doesn't inherit the session's scheme."""
    return bool(PRONOUN.search(text) or SCHEME_FACT.search(text))


async def classify(
    llm: Completer,
    part: Part,
    *,
    session_scheme: str | None,
    advice_refused: bool,
    min_confidence: float,
    sticky_min_confidence: float,
) -> Classification:
    text = part.text
    scheme, source = part.scheme, None
    if scheme is not None:
        source = "shared" if part.scheme_inherited else "part"
    elif session_scheme is not None and is_follow_up(text) and not names_other_scheme(text):
        scheme, source = session_scheme, "session"

    def result(intent: str, confidence: float, method: str, rule: str | None) -> Classification:
        return Classification(intent, confidence, method, rule, scheme, source, part.fee_type)

    rule = advice_rule(text)
    if rule:
        return result("advice", 1.0, "pre_rule", rule)
    rule = returns_rule(text)
    if rule:
        return result("returns", 1.0, "pre_rule", rule)
    if advice_refused and soft_advice(text):
        return result("advice", 1.0, "sticky_refusal", "soft_advice")

    # A follow-up ("what is its benchmark?") reads as vague on its own. The scheme it refers to
    # was resolved by code from another part or the session, so the model is told which.
    content = text
    if scheme is not None and source in ("shared", "session") and scheme in BY_SLUG:
        content = f"{text}\n(Here the customer means {BY_SLUG[scheme].name}, named earlier.)"
    out = await llm.complete(
        "intent",
        [{"role": "system", "content": INTENT_PROMPT}, {"role": "user", "content": content}],
        schema=_IntentOut,
        max_tokens=200,
    )
    parsed = out.parsed
    assert isinstance(parsed, _IntentOut)
    label = parsed.intent.strip().lower()
    if label not in INTENTS:
        return result("unclear", parsed.confidence, "low_confidence", label)
    if advice_refused and label == "advice" and parsed.confidence >= sticky_min_confidence:
        return result("advice", parsed.confidence, "sticky_refusal", "model_advice")
    if parsed.confidence < min_confidence and label not in REFUSAL_INTENTS:
        return result("unclear", parsed.confidence, "low_confidence", label)
    return result(label, parsed.confidence, "model", None)
