"""Routing note (architecture section 7.5). Deterministic, applied after drafting.

| Condition                                  | Team                 | Priority |
| PII present or high compliance risk        | compliance           | P1       |
| Pattern rising                             | product              | P1       |
| Pattern recurring, fee related             | support_l2_payments  | P2       |
| Pattern recurring, not fee related         | product              | P2       |
| Facts not covered (no canonical explainer) | support_l2_payments  | P2       |
| Otherwise                                  | support_l1           | P3       |

The "recurring, not fee related" row is an addition to the architecture's table: without it a
recurring product problem (11 reviews about the app crashing) fell through to "not covered"
and went to payments support.

Disagreement rule: when Facts (or a canonical explainer) answered but Pattern says recurring or
rising, the note states
the disagreement and the item is "reply and escalate" instead of "reply and close".
"""

from typing import Literal

from pydantic import BaseModel

from app.agents.compliance import ComplianceResult
from app.agents.facts import FactsResult
from app.agents.pattern import PatternResult
from app.weekly.fees import FEE_TYPES

Team = Literal["support_l1", "support_l2_payments", "product", "compliance"]
Priority = Literal["P1", "P2", "P3"]


class RoutingNote(BaseModel):
    team: Team
    priority: Priority
    reason: str
    disagreement: str | None = None
    action: Literal["reply_and_close", "reply_and_escalate"]


def fee_related(fee_type: str | None, pattern: PatternResult | None) -> bool:
    if fee_type:
        return True
    label = (pattern.matched_theme_label or "") if pattern else ""
    return any(f.matches(label) for f in FEE_TYPES)


def _evidence(p: PatternResult) -> str:
    return (f"{p.review_count + p.question_count} similar items "
            f"({p.review_count} reviews, {p.question_count} questions) "
            f"from {p.window_start:%d %b} to {p.window_end:%d %b}")  # fmt: skip


def route(
    *,
    facts: FactsResult | None,
    pattern: PatternResult | None,
    compliance: ComplianceResult | None,
    fee_type: str | None,
    canonical: bool = False,
) -> RoutingNote:
    """`canonical` is True when the reply is built from a canonical explainer: it then counts
    as answered, like a Facts answer."""
    team: Team
    priority: Priority
    if compliance is not None and compliance.risk_level == "high":
        team, priority, reason = "compliance", "P1", f"High compliance risk: {compliance.notes}"
    elif pattern is not None and pattern.label == "rising":
        team, priority = "product", "P1"
        reason = (f"Rising: {pattern.this_week} similar items this week vs "
                  f"{pattern.previous_week} the week before")  # fmt: skip
    elif pattern is not None and pattern.label == "recurring" and fee_related(fee_type, pattern):
        team, priority = "support_l2_payments", "P2"
        reason = f"Recurring fee issue: {_evidence(pattern)}"
    elif pattern is not None and pattern.label == "recurring":
        team, priority = "product", "P2"
        theme = f" (theme: {pattern.matched_theme_label})" if pattern.matched_theme_label else ""
        reason = f"Recurring issue{theme}: {_evidence(pattern)}"
    elif not canonical and facts is not None and facts.status == "not_covered":
        team, priority = "support_l2_payments", "P2"
        reason = "Not covered by the official documents; needs a person to answer"
    elif canonical:
        team, priority = "support_l1", "P3"
        reason = "Answered with the approved canonical explainer"
    else:
        team, priority, reason = "support_l1", "P3", "Answered from the official documents"

    answered = canonical or (facts is not None and facts.status == "answered")
    disagreement = None
    if answered and pattern is not None and pattern.label in ("recurring", "rising"):
        source = "The canonical explainer" if canonical else "Facts"
        disagreement = (f"{source} answered cleanly, but Pattern marks this {pattern.label}: "
                        f"{_evidence(pattern)}")  # fmt: skip
    action: Literal["reply_and_close", "reply_and_escalate"] = (
        "reply_and_escalate" if disagreement or priority == "P1" else "reply_and_close"
    )
    return RoutingNote(team=team, priority=priority, reason=reason, disagreement=disagreement,
                       action=action)  # fmt: skip
