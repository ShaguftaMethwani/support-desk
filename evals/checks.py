"""Small checks shared by the suites."""

import re
from typing import Any

ANSWERED = {"corpus", "canonical_explainer"}


def kind(answer: dict[str, Any]) -> str:
    """answered | refused | not_covered | clarification | blocked | unavailable | none"""
    if answer.get("status") == "unavailable":
        return "unavailable"
    d = answer.get("draft")
    if not d:
        return "none"
    if d.get("review_status") == "blocked" or answer.get("status") == "blocked":
        return "blocked"
    st = d.get("source_type")
    if st in ANSWERED:
        return "answered"
    return {"refusal": "refused", "not_covered": "not_covered"}.get(st, st or "none")


def norm(text: str) -> str:
    """Lowercase letters and digits only, so '98765 43210' and '98765-43210' compare equal."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def contains_raw(text: str, values: list[str]) -> list[str]:
    hay = norm(text)
    return [v for v in values if norm(v) and norm(v) in hay]


def problems_text(problems: list[str]) -> str:
    return "; ".join(problems)


def word_accuracy(expected: str, heard: str) -> float:
    """1 - word error rate (Levenshtein over words), floored at 0."""
    a = re.findall(r"[a-z0-9%]+", expected.lower())
    b = re.findall(r"[a-z0-9%]+", heard.lower())
    prev = list(range(len(b) + 1))
    for i, wa in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, wb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (wa != wb))
        prev = cur
    return max(0.0, 1 - prev[-1] / max(len(a), 1))


_NUMBER = re.compile(r"\d+(?:\.\d+)?")


def claim_numbers(text: str) -> list[str]:
    """Numbers stated in a reply body (dates and the Source/Last checked lines left out)."""
    body = "\n".join(
        ln for ln in text.splitlines() if not ln.startswith(("Source:", "Last checked:"))
    )
    body = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", " ", body)
    return [n for n in _NUMBER.findall(body) if n not in {"1", "2", "3"} or "." in n]


def number_in(number: str, text: str) -> bool:
    """A number appears in the source text, allowing '0.30' for '0.3' and '1,000' for '1000'."""
    plain = text.replace(",", "")
    variants = {number, number.rstrip("0").rstrip(".") if "." in number else number}
    if "." in number:
        variants.add(number + "0")
    return any(re.search(rf"(?<![\d.]){re.escape(v)}(?![\d])", plain) for v in variants)
