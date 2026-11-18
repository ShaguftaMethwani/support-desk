"""Intent classifier, incoming pre-rules and the labelled intent set."""

import json
from pathlib import Path

import pytest

from app.intent.classifier import classify, is_follow_up
from app.intent.rules import advice_rule, returns_rule
from app.intent.splitter import Part
from tests.weekly.conftest import ScriptedLLM

LABELLED = Path(__file__).resolve().parents[3] / "evals" / "datasets" / "intents_labelled.jsonl"
ROWS = [json.loads(line) for line in LABELLED.read_text().splitlines() if line]


def test_labelled_set_has_at_least_30_questions_covering_every_intent() -> None:
    assert len(ROWS) >= 30
    assert {r["intent"] for r in ROWS} == {
        "factual", "fee_canonical", "advice", "returns", "out_of_scope", "complaint", "unclear",
    }  # fmt: skip


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_pre_rules_on_the_labelled_set(row: dict[str, object]) -> None:
    """Rows marked pre_rule must be caught by code; every other row must not be refused by a
    rule (the factual rows include the known false-positive traps)."""
    text = str(row["text"])
    got = "advice" if advice_rule(text) else ("returns" if returns_rule(text) else None)
    assert got == (row["intent"] if row["pre_rule"] else None)


def model(intent: str, confidence: float) -> ScriptedLLM:
    return ScriptedLLM({"intent": [json.dumps({"intent": intent, "confidence": confidence})]})


async def run(
    text: str,
    llm: ScriptedLLM,
    *,
    scheme: str | None = None,
    session_scheme: str | None = None,
    refused: bool = False,
):  # type: ignore[no-untyped-def]
    return await classify(llm, Part(text, scheme), session_scheme=session_scheme,
                          advice_refused=refused, min_confidence=0.6,
                          sticky_min_confidence=0.3)  # fmt: skip


async def test_pre_rule_is_final_and_the_model_is_not_asked() -> None:
    llm = ScriptedLLM({})
    result = await run("Should I invest in HDFC Mid Cap Fund now?", llm)
    assert (result.intent, result.method, result.rule) == ("advice", "pre_rule", "should_i")
    assert llm.calls == []


async def test_model_label_and_low_confidence_becomes_unclear() -> None:
    assert (await run("What is the NAV?", model("factual", 0.9))).intent == "factual"
    low = await run("Tell me about the fund", model("factual", 0.4))
    assert (low.intent, low.method, low.rule) == ("unclear", "low_confidence", "factual")
    assert (await run("hmm", model("banana", 0.9))).intent == "unclear"


async def test_model_can_add_a_refusal_even_at_low_confidence() -> None:
    result = await run("Between the two, where does my money go?", model("advice", 0.5))
    assert result.intent == "advice"


async def test_sticky_refusal_catches_a_rephrased_advice_question() -> None:
    text = "Is putting money in mid cap a smart move?"  # no pre-rule matches this
    assert advice_rule(text) is None
    fresh = await run(text, model("factual", 0.8))
    assert fresh.intent == "factual"
    sticky = await run(text, ScriptedLLM({}), refused=True)
    assert (sticky.intent, sticky.method) == ("advice", "sticky_refusal")
    # A model "advice" label counts at lower confidence once the session has refused.
    low = await run("Where would the money do best?", model("advice", 0.35), refused=True)
    assert (low.intent, low.method) == ("advice", "sticky_refusal")


async def test_session_scheme_only_fills_follow_ups() -> None:
    follow = await run(
        "and what is its lock-in?", model("factual", 0.9), session_scheme="hdfc_elss_tax_saver"
    )
    assert (follow.scheme, follow.scheme_source) == ("hdfc_elss_tax_saver", "session")
    unrelated = await run("The app keeps crashing when I open my portfolio",
                          model("complaint", 0.9), session_scheme="hdfc_mid_cap")  # fmt: skip
    assert unrelated.scheme is None
    own = await run("lock-in of HDFC ELSS Tax Saver", model("factual", 0.9),
                    scheme="hdfc_elss_tax_saver", session_scheme="hdfc_mid_cap")  # fmt: skip
    assert (own.scheme, own.scheme_source) == ("hdfc_elss_tax_saver", "part")


def test_follow_up_detection() -> None:
    assert is_follow_up("and the exit load?") and is_follow_up("what is its NAV")
    assert not is_follow_up("my redemption is late")
