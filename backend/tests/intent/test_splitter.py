"""Question splitter: 25 split cases, plus the model fallback and its coverage check."""

import json

import pytest

from app.intent.splitter import MAX_PARTS, coverage_ok, split_message
from tests.weekly.conftest import ScriptedLLM

LIQ, ELSS, MID, FLEXI, LARGE = (
    "hdfc_liquid", "hdfc_elss_tax_saver", "hdfc_mid_cap", "hdfc_flexi_cap", "hdfc_large_cap",
)  # fmt: skip

# (message, expected part texts, expected scheme per part)
CASES: list[tuple[str, list[str], list[str | None]]] = [
    # single questions
    ("What is the exit load of HDFC Liquid Fund?",
     ["What is the exit load of HDFC Liquid Fund?"], [LIQ]),
    ("For HDFC Liquid Fund, what is the exit load?",
     ["For HDFC Liquid Fund, what is the exit load?"], [LIQ]),
    ("What is the exit load if I redeem HDFC Flexi Cap Fund within a year?",
     ["What is the exit load if I redeem HDFC Flexi Cap Fund within a year?"], [FLEXI]),
    ("How do I download my capital gains statement",
     ["How do I download my capital gains statement"], [None]),
    ("My redemption from HDFC Mid Cap has not arrived for 10 days and support is not replying",
     ["My redemption from HDFC Mid Cap has not arrived for 10 days and support is not replying"],
     [MID]),
    ("If I redeem my ELSS units, how is it taxed?",
     ["If I redeem my ELSS units, how is it taxed?"], [ELSS]),
    ("Can I start a SIP and also a lumpsum in the same folio?",  # tightly linked
     ["Can I start a SIP and also a lumpsum in the same folio?"], [None]),
    # question marks
    ("What is the benchmark of HDFC Mid Cap Fund? What is its riskometer level?",
     ["What is the benchmark of HDFC Mid Cap Fund?", "What is its riskometer level?"],
     [MID, MID]),
    ("Exit load of HDFC Liquid Fund? Lock-in of HDFC ELSS Tax Saver?",
     ["Exit load of HDFC Liquid Fund?", "Lock-in of HDFC ELSS Tax Saver?"], [LIQ, ELSS]),
    # joining words with a question on both sides
    ("What's the exit load on HDFC Liquid Fund, what's the ELSS lock-in, and should I redeem now?",
     ["What's the exit load on HDFC Liquid Fund", "what's the ELSS lock-in",
      "should I redeem now?"], [LIQ, ELSS, ELSS]),
    ("What is the minimum SIP for HDFC Large Cap Fund and what is its benchmark?",
     ["What is the minimum SIP for HDFC Large Cap Fund", "what is its benchmark?"],
     [LARGE, LARGE]),
    ("How do I start a SIP in HDFC Flexi Cap Fund; how do I stop it?",
     ["How do I start a SIP in HDFC Flexi Cap Fund", "how do I stop it?"], [FLEXI, FLEXI]),
    ("What is the NAV of HDFC Liquid Fund plus what is the exit load?",
     ["What is the NAV of HDFC Liquid Fund", "what is the exit load?"], [LIQ, LIQ]),
    ("Is there a lock-in for HDFC ELSS Tax Saver and can I withdraw early?",
     ["Is there a lock-in for HDFC ELSS Tax Saver", "can I withdraw early?"], [ELSS, ELSS]),
    # lists and line breaks
    ("1. exit load of HDFC Flexi Cap Fund\n2. minimum SIP for it\n3. lock-in of ELSS",
     ["exit load of HDFC Flexi Cap Fund", "minimum SIP for it", "lock-in of ELSS"],
     [FLEXI, FLEXI, ELSS]),
    ("- benchmark of HDFC Mid Cap Fund\n- benchmark of HDFC Large Cap Fund",
     ["benchmark of HDFC Mid Cap Fund", "benchmark of HDFC Large Cap Fund"], [MID, LARGE]),
    ("What is the exit load of HDFC Liquid Fund\nWhat is its expense ratio",
     ["What is the exit load of HDFC Liquid Fund", "What is its expense ratio"], [LIQ, LIQ]),
    # shared schemes: named once, applies to parts without one
    ("What is the exit load? And the expense ratio? Both for HDFC Mid Cap Fund.",
     ["What is the exit load?", "And the expense ratio? Both for HDFC Mid Cap Fund."],
     [MID, MID]),
    ("Tell me the benchmark. Is the HDFC Flexi Cap Fund open-ended?",
     ["Tell me the benchmark. Is the HDFC Flexi Cap Fund open-ended?"], [FLEXI]),
    # pronouns resolve to the nearest named scheme
    ("What is the lock-in of HDFC ELSS Tax Saver? What is the exit load of HDFC Liquid Fund? "
     "Is it charged on day 1?",
     ["What is the lock-in of HDFC ELSS Tax Saver?", "What is the exit load of HDFC Liquid Fund?",
      "Is it charged on day 1?"], [ELSS, LIQ, LIQ]),
    ("Does that one have a lock-in? I mean HDFC ELSS Tax Saver.",  # not a second question
     ["Does that one have a lock-in? I mean HDFC ELSS Tax Saver."], [ELSS]),
    # duplicates are merged
    ("What is the exit load of HDFC Liquid Fund? What is the exit load of HDFC Liquid Fund?",
     ["What is the exit load of HDFC Liquid Fund?"], [LIQ]),
    ("What is the benchmark of HDFC Mid Cap Fund? what is the benchmark of hdfc mid cap fund?",
     ["What is the benchmark of HDFC Mid Cap Fund?"], [MID]),
    # mixed intents stay separate parts
    ("What is the riskometer of HDFC Mid Cap Fund? Should I buy it?",
     ["What is the riskometer of HDFC Mid Cap Fund?", "Should I buy it?"], [MID, MID]),
    ("What returns did HDFC Large Cap Fund give? What is its benchmark?",
     ["What returns did HDFC Large Cap Fund give?", "What is its benchmark?"], [LARGE, LARGE]),
]  # fmt: skip


def test_there_are_25_cases() -> None:
    assert len(CASES) == 25


@pytest.mark.parametrize(("message", "parts", "schemes"), CASES)
async def test_split(message: str, parts: list[str], schemes: list[str | None]) -> None:
    result = await split_message(message)
    assert [p.text for p in result.parts] == parts
    assert [p.scheme for p in result.parts] == schemes
    for p in result.parts:  # parts are built from the message's own words
        assert coverage_ok(message, [p.text]) or p.text in message


async def test_more_than_five_parts_are_dropped_and_reported() -> None:
    lines = [f"What is the benchmark of {n}?" for n in (
        "HDFC Liquid Fund", "HDFC ELSS Tax Saver", "HDFC Mid Cap Fund", "HDFC Flexi Cap Fund",
        "HDFC Large Cap Fund")]  # fmt: skip
    lines += ["What is the exit load of HDFC Liquid Fund?", "What is the NAV of HDFC Mid Cap Fund?"]
    result = await split_message(" ".join(lines))
    assert len(result.parts) == MAX_PARTS
    assert result.dropped == lines[5:]


async def test_fee_type_is_shared_but_not_onto_a_part_with_its_own_topic() -> None:
    result = await split_message(
        "What's the exit load on HDFC Liquid Fund, what's the ELSS lock-in, "
        "and is it charged daily?"
    )
    assert [p.fee_type for p in result.parts] == ["exit_load", None, "exit_load"]


async def test_unsure_split_uses_the_model_with_a_coverage_check() -> None:
    message = "Tell me the exit load and expense ratio of HDFC Liquid Fund"
    llm = ScriptedLLM({"split": [json.dumps({"parts": [
        "exit load of HDFC Liquid Fund", "expense ratio of HDFC Liquid Fund"]})]})  # fmt: skip
    result = await split_message(message, llm)
    assert result.method == "model"
    assert [p.text for p in result.parts] == ["exit load of HDFC Liquid Fund",
                                              "expense ratio of HDFC Liquid Fund"]  # fmt: skip
    assert [p.fee_type for p in result.parts] == ["exit_load", "expense_ratio"]


async def test_model_parts_that_add_content_are_rejected() -> None:
    message = "Tell me the exit load and expense ratio of HDFC Liquid Fund"
    llm = ScriptedLLM({"split": [json.dumps({"parts": [
        "exit load of HDFC Liquid Fund", "should I buy HDFC Mid Cap Fund instead"]})]})  # fmt: skip
    result = await split_message(message, llm)
    assert result.method == "single" and [p.text for p in result.parts] == [message]


async def test_no_model_call_when_the_rules_are_sure() -> None:
    llm = ScriptedLLM({})  # any call would raise KeyError
    result = await split_message("What is the NAV of HDFC Liquid Fund? What is its AUM?", llm)
    assert result.method == "rules" and llm.calls == []


def test_coverage_check() -> None:
    original = "What is the exit load and expense ratio of HDFC Liquid Fund?"
    assert coverage_ok(
        original, ["exit load of HDFC Liquid Fund", "expense ratio of HDFC Liquid Fund"]
    )
    assert not coverage_ok(original, ["exit load of HDFC Mid Cap Fund"])  # new words
    assert not coverage_ok(original, ["exit load"])  # covers too little
