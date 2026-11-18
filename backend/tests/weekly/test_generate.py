"""Model-facing parts of the weekly loop, with a scripted model."""

import json
from datetime import date

import pytest

from app.policy import PolicyContext, RuleResult
from app.policy.text import count_words
from app.policy.validators import PULSE_MAX_WORDS
from app.weekly.fees import BY_KEY, FeeCount, FeeDetection
from app.weekly.generate import (
    FEE_DOC_TYPES,
    PolicyFailure,
    PulseInput,
    PulseTheme,
    QuoteCandidate,
    check_explainer,
    check_pulse,
    excerpt,
    explainer_context,
    explainer_passages,
    generate_validated,
    pick_quotes,
    quotable,
    quote_candidates,
    verify_quotes,
    write_explainer,
    write_pulse,
)
from tests.weekly.conftest import (
    EXIT_LOAD_CHUNKS,
    FakeRetriever,
    ScriptedLLM,
    chunk,
    explainer_handler,
    pulse_handler,
)

TODAY = date(2026, 11, 18)
DOMAINS = ("hdfcfund.com", "sebi.gov.in", "amfiindia.com")


# --- quotes -------------------------------------------------------------------------------


def test_excerpt_is_a_prefix_ending_at_a_sentence() -> None:
    long = "The app is slow to load. " * 12
    cut = excerpt(long)
    assert cut is not None and long.startswith(cut) and cut.endswith(".") and len(cut) <= 220
    assert excerpt("too short") is None
    assert excerpt("x" * 300) is None  # no sentence break to cut at


def test_quotes_with_redactions_advice_or_returns_are_not_offered() -> None:
    assert quotable("Payments fail every morning and support never replies.")
    assert not quotable("My PAN [PAN_REDACTED] was asked for on chat, terrible support.")
    assert not quotable("You should invest in this fund, it is the best for everyone.")
    assert not quotable("I made 40% returns in a year with this app, very happy.")


async def test_model_picks_ids_and_code_copies_verbatim_text() -> None:
    themes = [
        [("r1", "UPI payment failed and money was debited from my bank."),
         ("r2", "Payment stuck for days and no refund has arrived yet.")],
        [("r3", "Support never answers tickets and the bot is useless.")],
        [("r4", "The portfolio screen takes minutes to load every time.")],
    ]  # fmt: skip
    candidates = quote_candidates(themes)
    llm = ScriptedLLM({"quote_pick": [json.dumps({"picks": ["Q2", "Q3", "Q4", "Q99"]})]})
    quotes, how = await pick_quotes(llm, ["Payments", "Support", "Speed"], candidates)
    assert how == "model"
    assert [q.review_id for q in quotes] == ["r2", "r3", "r4"]
    stored = {rid: text for theme in themes for rid, text in theme}
    assert verify_quotes(quotes, stored) == []


async def test_invalid_picks_fall_back_to_one_quote_per_theme() -> None:
    candidates = [QuoteCandidate("a", 0, "first theme quote text here."),
                  QuoteCandidate("b", 0, "first theme second quote here."),
                  QuoteCandidate("c", 1, "second theme quote text here."),
                  QuoteCandidate("d", 2, "third theme quote text here.")]  # fmt: skip
    llm = ScriptedLLM({"quote_pick": [json.dumps({"picks": ["nope"]})]})
    quotes, how = await pick_quotes(llm, ["A", "B", "C"], candidates)
    assert how == "fallback"
    assert [q.review_id for q in quotes] == ["a", "c", "d"]


def test_verify_quotes_catches_text_not_in_the_review() -> None:
    from app.weekly.generate import Quote

    problems = verify_quotes([Quote("r1", 0, "invented words")], {"r1": "real review text"})
    assert problems == ["r1: quote is not a substring of the review"]


# --- generate_validated ---------------------------------------------------------------------


async def test_one_regeneration_names_the_failure_then_gives_up() -> None:
    seen: list[str | None] = []

    async def make(feedback: str | None) -> str:
        seen.append(feedback)
        return "bad"

    def check(_: str) -> list[RuleResult]:
        return [RuleResult("length_caps", False, "300 words (max 250)")]

    with pytest.raises(PolicyFailure, match="length_caps"):
        await generate_validated("weekly pulse", make, check)
    assert seen == [None, "- length_caps: 300 words (max 250)"]


async def test_regeneration_that_passes_is_used() -> None:
    outputs = iter(["bad", "good"])

    async def make(feedback: str | None) -> str:
        return next(outputs)

    def check(text: str) -> list[RuleResult]:
        return [RuleResult("x", text == "good", None)]

    result = await generate_validated("x", make, check)
    assert (result.value, result.attempts) == ("good", 2)
    assert result.log() == {"attempts": 2, "first_attempt_failed": [{"rule": "x", "detail": None}]}


# --- fee explainer --------------------------------------------------------------------------


async def test_explainer_retrieves_fee_documents_only() -> None:
    retriever = FakeRetriever(EXIT_LOAD_CHUNKS)
    passages = await explainer_passages(retriever, BY_KEY["exit_load"], min_similarity=0.65)
    assert {c["doc_types"] for c in retriever.calls} == {FEE_DOC_TYPES}
    assert "factsheet" not in FEE_DOC_TYPES
    assert [p.chunk_id for p in passages] == ["c1", "c2", "c3"]


async def test_no_source_takes_more_than_three_passages() -> None:
    many = [chunk(f"s{i}", "sebi_mf_faq", "https://www.sebi.gov.in/faq.pdf", "text", 0.9 - i / 100)
            for i in range(6)]  # fmt: skip
    many.append(chunk("h1", "hdfc_ter", "https://files.hdfcfund.com/ter.xls", "TER", 0.7))
    passages = await explainer_passages(
        FakeRetriever(many), BY_KEY["exit_load"], min_similarity=0.65
    )
    assert [p.chunk_id for p in passages] == ["s0", "s1", "s2", "h1"]


async def test_explainer_has_two_sources_and_code_set_last_checked() -> None:
    llm = ScriptedLLM({"fee_explainer": explainer_handler})
    draft = await write_explainer(llm, BY_KEY["exit_load"], EXIT_LOAD_CHUNKS)
    assert len(draft.bullets) == 3
    assert draft.source_urls == ("https://www.sebi.gov.in/faq.pdf",
                                 "https://files.hdfcfund.com/kim-liquid.pdf")  # fmt: skip
    assert draft.last_checked == date(2026, 11, 17)
    ctx = explainer_context(EXIT_LOAD_CHUNKS, DOMAINS, TODAY)
    assert all(r.passed for r in check_explainer(draft, ctx))


async def test_explainer_drops_unsupported_numbers_and_a_third_source() -> None:
    out = {"bullets": [
        {"text": "Exit load is charged when units are redeemed early.", "passage": 1},
        {"text": "The exit load is 2% for a year.", "passage": 2},  # 2 is not in passage 2
        {"text": "It is 0.0070% on Day 1.", "passage": 2},
        {"text": "Read the scheme documents carefully.", "passage": 3},  # third source
        {"text": "Bad passage number.", "passage": 9},
    ]}  # fmt: skip
    llm = ScriptedLLM({"fee_explainer": [json.dumps(out)]})
    draft = await write_explainer(llm, BY_KEY["exit_load"], EXIT_LOAD_CHUNKS)
    assert draft.bullets == ("Exit load is charged when units are redeemed early.",
                             "It is 0.0070% on Day 1.")  # fmt: skip
    assert draft.dropped_bullets == 3
    assert len(draft.source_urls) == 2


async def test_single_source_explainer_fails_policy() -> None:
    out = {"bullets": [{"text": "Exit load is a charge on early redemption.", "passage": 1}]}
    llm = ScriptedLLM({"fee_explainer": [json.dumps(out)]})
    draft = await write_explainer(llm, BY_KEY["exit_load"], EXIT_LOAD_CHUNKS)
    ctx = explainer_context(EXIT_LOAD_CHUNKS, DOMAINS, TODAY)
    failed = [r.rule for r in check_explainer(draft, ctx) if not r.passed]
    assert "explainer_sources" in failed


# --- pulse ----------------------------------------------------------------------------------


def _pulse_input(fee_type: str | None = "exit_load") -> PulseInput:
    counts = [FeeCount("exit_load", "Exit load on early redemption", True, ["a", "b", "c"], 4.5),
              FeeCount("brokerage", "Brokerage charges", False, ["d"], 1.0)]  # fmt: skip
    fees = FeeDetection(
        fee_type, "Exit load on early redemption" if fee_type else None,
        ["a", "b", "c"] if fee_type else [], counts, 3,
    )  # fmt: skip
    return PulseInput(
        window_start=date(2026, 8, 27), window_end=TODAY, reviews_analysed=1000,
        reviews_themed=450,
        themes=[PulseTheme("Payment and UPI failures", "Payments fail.", 120),
                PulseTheme("Unresponsive customer support", "No replies.", 95),
                PulseTheme("Slow and crashing app", "Slow app.", 80)],
        quotes=[("UPI payment failed and money was debited.", "Payment and UPI failures")],
        fees=fees, explainer_attached=fee_type is not None,
    )  # fmt: skip


async def test_pulse_counts_quotes_and_fee_line_are_written_by_code() -> None:
    llm = ScriptedLLM({"weekly_pulse": pulse_handler})
    text = await write_pulse(llm, _pulse_input())
    assert "- Payment and UPI failures (120 reviews): Payments fail while money is debited." in text
    assert '- "UPI payment failed and money was debited." (Payment and UPI failures)' in text
    assert "Fee confusion: Exit load on early redemption (3 reviews)." in text
    assert "Charge complaints outside mutual funds (reviews): brokerage charges (1)." in text
    assert text.count("\n1. ") == text.count("\n2. ") == text.count("\n3. ") == 1
    assert count_words(text) <= PULSE_MAX_WORDS
    assert all(r.passed for r in check_pulse(text, PolicyContext(DOMAINS, TODAY)))


async def test_pulse_without_fee_issue_says_so() -> None:
    llm = ScriptedLLM({"weekly_pulse": pulse_handler})
    text = await write_pulse(llm, _pulse_input(fee_type=None))
    assert "Fee confusion: none detected this week." in text
    assert "explainer" not in text


async def test_overlong_pulse_fails_the_length_cap() -> None:
    long = json.loads(pulse_handler([]))
    long["summary"] = "word " * 300
    llm = ScriptedLLM({"weekly_pulse": [json.dumps(long)]})
    text = await write_pulse(llm, _pulse_input())
    failed = [r.rule for r in check_pulse(text, PolicyContext(DOMAINS, TODAY)) if not r.passed]
    assert failed == ["length_caps"]


async def test_pulse_needs_exactly_three_actions() -> None:
    from pydantic import ValidationError

    two = json.loads(pulse_handler([]))
    two["actions"] = two["actions"][:2]
    llm = ScriptedLLM({"weekly_pulse": [json.dumps(two)]})
    with pytest.raises(ValidationError):
        await write_pulse(llm, _pulse_input())


def test_chunk_helper_builds_retrieved_chunks() -> None:
    c = chunk("x", "k", "https://www.sebi.gov.in/x", "text")
    assert c.similarity == c.score == 0.8
