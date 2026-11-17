"""Which model tier each task uses (architecture section 4.1).

Small: high-volume, simple classification. Large: drafting that must stay inside sources.
"""

from app.config import Tier


class UnknownTaskError(ValueError):
    pass


TASK_TIERS: dict[str, Tier] = {
    "intent": Tier.SMALL,
    "split": Tier.SMALL,
    "refine_classify": Tier.SMALL,
    "theme_label": Tier.SMALL,
    "compliance_check": Tier.SMALL,
    "facts": Tier.LARGE,
    "orchestrate": Tier.LARGE,
    "review_judge": Tier.LARGE,
    "weekly_pulse": Tier.LARGE,
    "fee_explainer": Tier.LARGE,
}


def tier_for(task: str) -> Tier:
    try:
        return TASK_TIERS[task]
    except KeyError as exc:
        raise UnknownTaskError(f"No model tier configured for task {task!r}") from exc
