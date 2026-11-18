"""Loop connector pieces that need no database: the pulse's support-queue section and its
check, the routing of canonical answers, and the Doc entry text."""

import dataclasses
import json
from datetime import date

from app.agents.facts import FactsResult
from app.agents.pattern import PatternResult
from app.mcp.mapping import queue_doc_text, weekly_doc_text
from app.orchestrator.routing import route
from app.policy import PolicyContext
from app.policy.text import count_words
from app.policy.validators import PULSE_MAX_WORDS
from app.weekly.generate import (
    SupportTheme,
    check_pulse,
    support_themes_reported,
    write_pulse,
)
from tests.weekly.conftest import ScriptedLLM, pulse_handler
from tests.weekly.test_generate import DOMAINS, TODAY, _pulse_input

SUPPORT = [
    SupportTheme("Exit load on redemption", 7, date(2026, 11, 12), date(2026, 11, 18)),
    SupportTheme("Statement downloads", 5, date(2026, 11, 14), date(2026, 11, 17)),
]


# --- pulse ----------------------------------------------------------------------------------


async def test_pulse_lists_each_support_queue_cluster_with_count_and_window() -> None:
    llm = ScriptedLLM({"weekly_pulse": pulse_handler})
    inp = dataclasses.replace(_pulse_input(), support_themes=SUPPORT)
    text = await write_pulse(llm, inp)
    assert "From the support queue (similar customer questions):" in text
    assert "- Exit load on redemption (7 questions, 12 Nov to 18 Nov)" in text
    assert "- Statement downloads (5 questions, 14 Nov to 17 Nov)" in text
    assert count_words(text) <= PULSE_MAX_WORDS
    results = check_pulse(text, PolicyContext(DOMAINS, TODAY), SUPPORT)
    assert all(r.passed for r in results)
    # The model is told what customers keep asking, so its action ideas can use it.
    _, messages = llm.calls[0]
    assert "Customers keep asking support about:" in messages[-1]["content"]


def test_a_pulse_missing_a_surfaced_cluster_fails_the_check() -> None:
    text = "Weekly Product Pulse\n- Exit load on redemption (7 questions, 12 Nov to 18 Nov)"
    result = support_themes_reported(text, SUPPORT)
    assert not result.passed and "Statement downloads" in (result.detail or "")
    # A changed count is missing too: the pulse must state the cluster's own count.
    wrong = text.replace("(7 questions", "(6 questions")
    assert not support_themes_reported(wrong, SUPPORT[:1]).passed
    assert support_themes_reported("anything", []).passed


async def test_pulse_without_surfaced_clusters_has_no_support_section() -> None:
    llm = ScriptedLLM({"weekly_pulse": pulse_handler})
    text = await write_pulse(llm, _pulse_input())
    assert "support queue" not in text


# --- routing --------------------------------------------------------------------------------


def _pattern(label: str) -> PatternResult:
    return PatternResult(
        label=label, review_count=6, question_count=1, this_week=2, previous_week=1,
        window_start=date(2026, 10, 22), window_end=date(2026, 11, 18),
    )  # type: ignore[arg-type]  # fmt: skip


def test_a_canonical_answer_counts_as_answered() -> None:
    note = route(facts=None, pattern=_pattern("one_off"), compliance=None, fee_type="exit_load",
                 canonical=True)  # fmt: skip
    assert note.team == "support_l1" and note.priority == "P3"
    assert note.reason == "Answered with the approved canonical explainer"
    assert note.action == "reply_and_close"


def test_a_recurring_canonical_answer_is_escalated_with_the_disagreement() -> None:
    note = route(facts=None, pattern=_pattern("recurring"), compliance=None,
                 fee_type="exit_load", canonical=True)  # fmt: skip
    assert note.team == "support_l2_payments" and note.action == "reply_and_escalate"
    assert note.disagreement and note.disagreement.startswith("The canonical explainer answered")


def test_without_a_canonical_answer_not_covered_still_goes_to_payments() -> None:
    facts = FactsResult.model_validate(
        {"status": "not_covered", "answer": None, "citation": None, "searched": []}
    )
    note = route(facts=facts, pattern=None, compliance=None, fee_type=None)
    assert note.team == "support_l2_payments"


# --- Doc entries ----------------------------------------------------------------------------


def test_weekly_doc_entry_lists_support_queue_themes_with_item_ids() -> None:
    payload = {
        "date": "2026-11-18",
        "window": {"start": "2026-08-27", "end": "2026-11-18"},
        "top_themes": [],
        "support_queue": [
            {
                "label": "Exit load on redemption",
                "count": 2,
                "window": {"start": "2026-11-12", "end": "2026-11-18"},
                "question_ids": ["q-1", "q-2"],
            },
        ],  # fmt: skip
        "weekly_pulse": "Pulse.",
        "identified_fee_issue": "None",
    }
    text = "\n".join(weekly_doc_text(json.loads(json.dumps(payload))))
    assert "- Exit load on redemption (2 questions, 2026-11-12 to 2026-11-18)" in text
    assert "Queue items: q-1, q-2" in text


def test_queue_doc_entry_names_the_canonical_explainer() -> None:
    payload = {"date": "2026-11-18", "question": "Q?", "reply": "R.", "citation": None,
               "routing": None, "canonical_explainer_id": "c-1"}  # fmt: skip
    assert "Canonical answer: explainer c-1" in queue_doc_text(payload)
    del payload["canonical_explainer_id"]
    assert not any("Canonical" in line for line in queue_doc_text(payload))
