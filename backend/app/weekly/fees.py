"""Fee confusion detector (architecture section 8.2). Code only, no model calls.

1. Each review is matched against a fee lexicon, grouped by fee type.
2. Each fee type is scored by the reviews that mention it, with extra weight for low ratings.
3. The top *mutual fund* fee type with at least `min_reviews` reviews is the week's fee issue,
   with its evidence review ids.
4. If none qualifies, the run records "no fee confusion detected this week" and no explainer is
   generated. The detector never invents one.

Two kinds of fee type:
- Mutual fund fees (exit load, expense ratio and Direct vs Regular plans, stamp duty, STT).
  The corpus covers these, so they can get an explainer.
- Platform charges (brokerage, currency conversion, other or hidden charges). These are about
  INDMoney's own pricing for stocks and US investing, which the corpus does not cover. They
  are counted and reported in the pulse as observations, but never get an explainer.

Generic deduction words ("deducted", "less amount", "cut") count as exit load only when the
same review talks about redeeming a mutual fund; elsewhere they are a platform charge.
"""

import re
from dataclasses import dataclass, field
from typing import Any

LOW_RATING = 2
LOW_RATING_WEIGHT = 1.5


@dataclass(frozen=True)
class FeeType:
    key: str
    label: str
    mutual_fund: bool  # mutual fund fee the corpus can explain
    patterns: tuple[str, ...]
    # Questions used to retrieve explainer passages (mutual fund fees only).
    queries: tuple[str, ...] = ()

    def matches(self, text: str) -> bool:
        return any(re.search(p, text, re.IGNORECASE) for p in self.patterns)


_MF_CONTEXT = r"mutual\s*funds?|\bmfs?\b|\bsips?\b|\bnav\b|\bunits\b"
_REDEEM = r"redeem|redemption|withdraw|\bsold\b|\bsell(ing)?\b|\bexit(ed)?\b"
_DEDUCTION = r"deduct|less\s+amount|\bcut\b|charged|\bcharges?\b"

FEE_TYPES: tuple[FeeType, ...] = (
    FeeType(
        "exit_load",
        "Exit load on early redemption",
        True,
        (r"exit\s*-?\s*loads?", r"early\s+(redemption|withdrawal|exit)\s+(charge|fee|penalty)"),
        (
            "What is an exit load and when is it charged on redemption?",
            "Exit load if units are redeemed within one year of allotment",
        ),
    ),
    FeeType(
        "expense_ratio",
        "Expense ratio and Direct vs Regular plans",
        True,
        (
            r"expense\s*ratio",
            r"\bTER\b",
            r"\bregular\s+(plans?|funds?|sifs?|options?)\b",
            r"\bdirect\s+(plans?|growth|funds?|sifs?|mutual\s*funds?)\b",
            r"\bcommissions?\b",
        ),
        (
            "What is the total expense ratio of a mutual fund scheme and what does it include?",
            "Why does the Direct Plan have a lower expense ratio than the Regular Plan?",
            "Total expense ratio of the Regular Plan and the Direct Plan of HDFC schemes",
        ),
    ),
    FeeType(
        "stamp_duty",
        "Stamp duty on mutual fund purchases",
        True,
        (r"stamp\s*duty",),
        ("Is stamp duty charged when buying mutual fund units?",),
    ),
    FeeType(
        "stt",
        "Securities transaction tax (STT)",
        True,
        (r"\bSTT\b", r"securities\s+transaction\s+tax"),
        ("Is securities transaction tax charged on mutual fund redemption?",),
    ),
    FeeType("brokerage", "Brokerage charges", False, (r"brokerage", r"brokrage")),
    FeeType(
        "currency_conversion",
        "Currency conversion for US investing",
        False,
        (r"\bconversion\b", r"exchange\s+rate", r"\bforex\b", r"\bfx\b"),
    ),
    FeeType(
        "other_charges",
        "Other or hidden charges",
        False,
        (r"hidden\s+(charges?|costs?|fees?)", r"\bcharges?\b", r"\bfees?\b", _DEDUCTION),
    ),
)
BY_KEY = {f.key: f for f in FEE_TYPES}


@dataclass(frozen=True)
class FeeReview:
    review_id: str
    rating: int | None
    text: str


@dataclass
class FeeCount:
    key: str
    label: str
    mutual_fund: bool
    review_ids: list[str] = field(default_factory=list)
    score: float = 0.0

    @property
    def count(self) -> int:
        return len(self.review_ids)


@dataclass(frozen=True)
class FeeDetection:
    fee_type: str | None  # None: no fee confusion this week
    label: str | None
    evidence_review_ids: list[str]
    counts: list[FeeCount]  # every fee type with at least one mention, highest score first
    min_reviews: int

    @property
    def message(self) -> str:
        if self.fee_type is None:
            return "No fee confusion detected this week."
        return f"{self.label}: {len(self.evidence_review_ids)} reviews"

    def as_dict(self) -> dict[str, Any]:
        return {
            "fee_type": self.fee_type,
            "label": self.label,
            "message": self.message,
            "evidence_review_ids": self.evidence_review_ids,
            "min_reviews": self.min_reviews,
            "counts": [
                {
                    "fee_type": c.key,
                    "label": c.label,
                    "mutual_fund": c.mutual_fund,
                    "reviews": c.count,
                    "score": round(c.score, 2),
                    "review_ids": c.review_ids,
                }
                for c in self.counts
            ],  # fmt: skip
        }


def fee_types_in(text: str) -> list[str]:
    """Fee types a review mentions. "Other charges" only when nothing more specific matched."""
    found = [f.key for f in FEE_TYPES if f.key != "other_charges" and f.matches(text)]
    if (
        "exit_load" not in found
        and re.search(_DEDUCTION, text, re.I)
        and re.search(_REDEEM, text, re.I)
        and re.search(_MF_CONTEXT, text, re.I)
    ):
        found.append("exit_load")
    if not found and BY_KEY["other_charges"].matches(text):
        found.append("other_charges")
    return found


def detect_fee_confusion(reviews: list[FeeReview], *, min_reviews: int) -> FeeDetection:
    counts = {f.key: FeeCount(f.key, f.label, f.mutual_fund) for f in FEE_TYPES}
    for review in reviews:
        weight = (
            LOW_RATING_WEIGHT if review.rating is not None and review.rating <= LOW_RATING else 1.0
        )
        for key in fee_types_in(review.text):
            counts[key].review_ids.append(review.review_id)
            counts[key].score += weight
    mentioned = sorted(
        (c for c in counts.values() if c.count),
        key=lambda c: (-c.score, -c.count, c.key),
    )
    qualifying = [c for c in mentioned if c.mutual_fund and c.count >= min_reviews]
    if not qualifying:
        return FeeDetection(None, None, [], mentioned, min_reviews)
    top = qualifying[0]
    return FeeDetection(top.key, top.label, list(top.review_ids), mentioned, min_reviews)
