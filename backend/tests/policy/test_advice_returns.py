"""Advice and returns cases (Phase 1 asks for at least 40).

Every false-positive decision behind these cases is documented in docs/guardrails.md.
"""

import pytest

from app.policy.types import Scope
from app.policy.validators import advice_guard, returns_guard

ADVICE_BLOCKED = [
    "You should redeem your units now.",
    "You should not invest in this fund.",
    "We recommend switching to the direct plan.",
    "I would suggest holding the fund for longer.",
    "Staying invested longer may help you avoid exit load.",
    "Consider investing through a SIP.",
    "It's a good time to buy.",
    "This is a great fund for beginners.",
    "HDFC Flexi Cap is a good investment.",
    "In my opinion, the mid cap fund is risky.",
    "If I were you, I would redeem.",
    "You may want to stop your SIP.",
    "It is advisable to stay invested.",
    "The best fund for tax saving is the ELSS fund.",
    "Buy now before the NAV rises.",
    "This fund is a safe bet.",
    "SIPs will help you grow your wealth.",
    "It's worth investing in this scheme.",
    "You might want to consider switching to the direct plan.",
    "It is recommended that you hold the units.",
]

RETURNS_BLOCKED = [
    "The fund returned 14% last year.",
    "It has delivered 18.5% CAGR over five years.",
    "You can expect returns of around 12%.",
    "Guaranteed returns with no risk.",
    "The scheme outperformed its benchmark.",
    "3-year return is 21%.",
    "This fund will double your money.",
    "The NAV is expected to grow.",
    "It is a top-performing fund.",
    "Returns of 9% p.a. are typical for liquid funds.",
    "TER is 0.67% and the fund has given 15% returns.",
    "Since inception returns are strong.",
    "The fund grew 22 per cent in 2025.",
]

ALLOWED = [
    "You should receive your statement by email within 5 working days.",
    "The exit load is 1% if units are redeemed within 1 year.",
    "The expense ratio of the Direct Plan is 0.67%.",
    "ELSS units are locked in for 3 years from the date of allotment.",
    "You can redeem units online through the app after the lock-in ends.",
    "Long-term capital gains above ₹1.25 lakh are taxed at 12.5%.",
    "The fund's past performance is available in the latest factsheet.",
    "The benchmark for HDFC Large Cap Fund is NIFTY 100 TRI.",
    "Stamp duty of 0.005% applies on purchases.",
    "Exit load for the liquid fund ranges from 0.0070% on day 1 to nil from day 7.",
    "The minimum SIP amount is ₹100.",
    "The riskometer level for this scheme is Very High.",
    "You need to complete KYC before investing.",
    "Please consult a SEBI-registered investment adviser for advice on your portfolio.",
    "I can't help with recommendations, but the scheme's facts are in the SID.",
    "Choose the Growth option or IDCW option at the time of investing.",
    "HDFC ELSS Tax Saver - Direct Plan - Growth has a 3-year lock-in.",
    "The NAV of the Growth option is published daily on the AMC website.",
    "STT is not charged on mutual fund purchases.",
    "Short-term capital gains are taxed at 20%.",
]


def test_case_count_meets_plan() -> None:
    assert len(ADVICE_BLOCKED) + len(RETURNS_BLOCKED) + len(ALLOWED) >= 40


@pytest.mark.parametrize("text", ADVICE_BLOCKED)
def test_advice_is_blocked(text: str) -> None:
    result = advice_guard(text, Scope.REPLY)
    assert not result.passed
    assert result.detail and ":" in result.detail  # names the rule that fired


@pytest.mark.parametrize("text", RETURNS_BLOCKED)
def test_returns_are_blocked(text: str) -> None:
    assert not returns_guard(text).passed


@pytest.mark.parametrize("text", ALLOWED)
def test_factual_text_is_allowed(text: str) -> None:
    assert advice_guard(text, Scope.REPLY).passed
    assert returns_guard(text).passed


def test_pulse_may_recommend_product_changes() -> None:
    text = "We recommend adding an exit-load warning to the redemption screen."
    assert advice_guard(text, Scope.PULSE).passed
    assert not advice_guard(text, Scope.REPLY).passed


def test_pulse_still_blocks_investment_advice() -> None:
    assert not advice_guard("Users should stay invested to avoid exit load.", Scope.PULSE).passed


# Decisions documented in docs/guardrails.md. If one of these changes, update that file too.
DOCUMENTED_FALSE_POSITIVES = [
    ("You should hold the units for 3 years due to the lock-in.", "directive_investment_action"),
    ("We recommend you contact support.", "first_person_recommendation"),
    ("It is better to keep your KYC updated.", "advisable_to"),
    ("Online redemption is a good option if you need money quickly.", "product_judgement"),
    ("Investors who stay invested for 3 years see lower costs.", "stay_invested"),
]
DOCUMENTED_REPHRASINGS = [
    "ELSS units are locked in for 3 years.",
    "Please contact support.",
    "KYC must be up to date to transact.",
    "Redemptions can be placed online.",
    "You may want to consider the factsheet.",
]
DOCUMENTED_KNOWN_MISSES = [
    "Many investors prefer the direct plan.",
    "Long holding periods have historically suited equity funds.",
]


@pytest.mark.parametrize(("text", "rule"), DOCUMENTED_FALSE_POSITIVES)
def test_documented_false_positives_stay_blocked(text: str, rule: str) -> None:
    result = advice_guard(text, Scope.REPLY)
    assert not result.passed and result.detail and result.detail.startswith(rule)


@pytest.mark.parametrize("text", DOCUMENTED_REPHRASINGS + DOCUMENTED_KNOWN_MISSES)
def test_documented_rephrasings_and_known_misses_pass(text: str) -> None:
    assert advice_guard(text, Scope.REPLY).passed


def test_documented_refusal_wording() -> None:
    assert not returns_guard("I can't share expected returns.").passed
    approved = (
        "I can't share return or performance figures. "
        "The latest factsheet has the scheme's official performance data."
    )
    assert returns_guard(approved).passed
