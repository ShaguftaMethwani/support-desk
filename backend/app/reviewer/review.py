"""Reviewer (architecture section 7.6).

The policy engine's validators decide. For replies the model wrote, a large-model judge gives
a second opinion on whether the text reads as advice. The judge can only add a failure; it
never clears one raised by the validators, and it is only asked when the validators pass.
"""

from collections.abc import Mapping, Sequence
from typing import Protocol

from pydantic import BaseModel

from app.llm import LLMError, LLMResult
from app.policy import PolicyContext, RuleResult, run_policy
from app.policy.types import ReplyArtifact

HARD_FAILS = frozenset({"citation_required", "advice_guard", "returns_guard", "pii_guard"})


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


JUDGE_PROMPT = """You review a reply an investing app's support desk will send to a customer.
Replies must be facts only.

Does the reply give investment advice (tell the customer what to buy, sell, hold or choose,
or that something is a good or bad investment), or discuss returns or performance?
Stating a fee, a rule or a process is NOT advice.

Reply with JSON only: {"reads_as_advice": true|false, "reason": "..."}"""


class _JudgeOut(BaseModel):
    reads_as_advice: bool
    reason: str = ""


async def judge(llm: Completer, text: str) -> RuleResult:
    try:
        out = await llm.complete(
            "review_judge",
            [{"role": "system", "content": JUDGE_PROMPT}, {"role": "user", "content": text}],
            schema=_JudgeOut,
            max_tokens=300,
        )
    except LLMError:
        # No second opinion available: the deterministic validators already passed.
        return RuleResult("advice_judge", True, "judge unavailable")
    parsed = out.parsed
    if isinstance(parsed, _JudgeOut) and parsed.reads_as_advice:
        return RuleResult("advice_judge", False, parsed.reason[:200] or "reads as advice")
    return RuleResult("advice_judge", True)


async def review_reply(
    artifact: ReplyArtifact, ctx: PolicyContext, *, llm: Completer | None, use_judge: bool
) -> list[RuleResult]:
    results = run_policy(artifact, ctx)
    if use_judge and llm is not None and all(r.passed for r in results):
        results.append(await judge(llm, artifact.text))
    return results
