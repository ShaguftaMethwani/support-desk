"""Compliance Agent (architecture section 7.4): scans the incoming question. Code first.

- PII: the ingress guard already redacted the text; its typed tokens ("[PAN_REDACTED]") show
  what was found. Identity and account data (PAN, Aadhaar, account, OTP, UPI, long id numbers)
  is high risk; phone and email are medium. Names are noted but low risk: the name detector
  also redacts ordinary Hindi and Hinglish words, so names alone don't escalate.
- Advice and returns seeking: the intent pre-rules. A match is medium risk.
- Inconclusive: only soft advice wording ("is it a smart move") and no rule match. Then, and
  only then, the small model is asked.
The outgoing draft is checked by the same validators inside the reviewer.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, Field

from app.intent.rules import advice_rule, returns_rule, soft_advice
from app.llm import LLMError, LLMResult

_TOKEN = re.compile(r"\[([A-Z_]+)_REDACTED\]")
HIGH_RISK_PII = frozenset({"pan", "aadhaar", "account", "otp", "upi", "id_number"})
MEDIUM_RISK_PII = frozenset({"phone", "email"})


class Flag(BaseModel):
    type: str  # pii | advice_seeking | returns_seeking
    detail: str
    source: Literal["guard", "rule", "model"]


class ComplianceResult(BaseModel):
    incoming_flags: list[Flag] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"]
    notes: str
    model_used: bool = False


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


CHECK_PROMPT = """Does this message to an investing app's support desk ask for investment
advice (what to buy, sell, hold or choose) or about returns or performance?

Reply with JSON only: {"advice_seeking": true|false, "returns_seeking": true|false}"""


class _CheckOut(BaseModel):
    advice_seeking: bool = False
    returns_seeking: bool = False


def pii_types(text: str) -> list[str]:
    return sorted({m.group(1).lower() for m in _TOKEN.finditer(text)})


async def run_compliance(text: str, llm: Completer | None = None) -> ComplianceResult:
    flags = [Flag(type="pii", detail=t, source="guard") for t in pii_types(text)]
    rule = advice_rule(text)
    if rule:
        flags.append(Flag(type="advice_seeking", detail=rule, source="rule"))
    returns = returns_rule(text)
    if returns:
        flags.append(Flag(type="returns_seeking", detail=returns, source="rule"))
    model_used = False
    unavailable = False
    if not rule and not returns and soft_advice(text) and llm is not None:
        try:
            out = await llm.complete(
                "compliance_check",
                [{"role": "system", "content": CHECK_PROMPT}, {"role": "user", "content": text}],
                schema=_CheckOut,
                max_tokens=150,
            )
            model_used = True
            parsed = out.parsed
            if isinstance(parsed, _CheckOut):
                if parsed.advice_seeking:
                    flags.append(Flag(type="advice_seeking", detail="model", source="model"))
                if parsed.returns_seeking:
                    flags.append(Flag(type="returns_seeking", detail="model", source="model"))
        except LLMError:
            unavailable = True  # noted, not guessed: the reviewer still checks the draft

    pii = {f.detail for f in flags if f.type == "pii"}
    seeking = [f for f in flags if f.type != "pii"]
    if pii & HIGH_RISK_PII:
        risk: Literal["low", "medium", "high"] = "high"
    elif pii & MEDIUM_RISK_PII or seeking:
        risk = "medium"
    else:
        risk = "low"
    notes = (
        "; ".join([f"PII redacted at intake: {', '.join(sorted(pii))}"] if pii else [])
        or "no PII found"
    )
    if seeking:
        notes += "; asks for " + " and ".join(
            sorted({f.type.replace("_seeking", "") for f in seeking})
        )
    if unavailable:
        notes += "; model check unavailable"
    return ComplianceResult(incoming_flags=flags, risk_level=risk, notes=notes,
                            model_used=model_used)  # fmt: skip
