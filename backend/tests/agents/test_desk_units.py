"""Compliance Agent, routing, refinement feedback and reply drafting: code-only parts."""

import json
from datetime import date

import pytest

from app.agents.compliance import run_compliance
from app.agents.facts import FactsResult, SourceCitation
from app.agents.pattern import PatternResult, PatternSettings, label_for
from app.orchestrator.draft import (
    ADVICE_REFUSAL,
    COMPLAINT_NOT_COVERED,
    NOT_COVERED,
    RETURNS_REFUSAL,
    Draft,
    clarification,
    write_answer,
)
from app.orchestrator.refine import AGENTS_FOR, classify_feedback, feedback_by_rules
from app.orchestrator.routing import RoutingNote, route
from app.policy import InMemoryCorpusIndex, PolicyContext, SourceType, run_policy
from tests.weekly.conftest import ScriptedLLM

TODAY = date(2026, 11, 18)
CITATION = SourceCitation(chunk_id="c1", source_key="sebi_mf_faq",
                          url="https://www.sebi.gov.in/faq.pdf", title="FAQ", page=8,
                          last_checked=date(2026, 11, 17))  # fmt: skip
ROUTING = RoutingNote(team="support_l1", priority="P3", reason="r", action="reply_and_close")


def pattern(label: str, reviews: int = 0, theme: str | None = None) -> PatternResult:
    return PatternResult(label=label, matched_theme_label=theme, review_count=reviews,  # type: ignore[arg-type]
                         question_count=0, this_week=0, previous_week=0,
                         window_start=date(2026, 10, 22), window_end=TODAY)  # fmt: skip


ANSWERED = FactsResult(status="answered", answer="Exit load is 0.0070% on day 1.",
                       citation=CITATION)  # fmt: skip
NOT_COVERED_FACTS = FactsResult(status="not_covered", reason="low_similarity")


# --- compliance -----------------------------------------------------------------------------


async def test_high_risk_pii_from_guard_tokens_without_a_model_call() -> None:
    llm = ScriptedLLM({})
    result = await run_compliance("My PAN is [PAN_REDACTED], where is my money?", llm)
    assert result.risk_level == "high" and not result.model_used
    assert [(f.type, f.detail) for f in result.incoming_flags] == [("pii", "pan")]


async def test_names_alone_are_low_risk() -> None:
    result = await run_compliance("[NAME_REDACTED] from support was helpful", ScriptedLLM({}))
    assert result.risk_level == "low"


async def test_advice_seeking_by_rule_and_model_only_when_inconclusive() -> None:
    llm = ScriptedLLM({})
    ruled = await run_compliance("Should I sell HDFC Mid Cap Fund?", llm)
    assert ruled.risk_level == "medium" and ruled.incoming_flags[0].source == "rule"
    soft = ScriptedLLM({"compliance_check": [json.dumps({"advice_seeking": True})]})
    asked = await run_compliance("Is mid cap a smart move right now?", soft)
    assert asked.model_used and asked.incoming_flags[0].source == "model"
    plain = await run_compliance("What is the exit load?", llm)
    assert plain.risk_level == "low" and llm.calls == []


# --- pattern labels and routing -------------------------------------------------------------


def test_pattern_labels() -> None:
    cfg = PatternSettings(0.74, 0.77, 28, 5, 3)
    assert label_for(4, 3, 1, cfg) == "rising"
    assert label_for(6, 1, 1, cfg) == "recurring"
    assert label_for(4, 2, 0, cfg) == "one_off"  # 2 is below the rising minimum


@pytest.mark.parametrize(
    ("facts", "pat", "risk", "fee", "team", "priority", "action"),
    [
        (ANSWERED, pattern("one_off"), "high", None, "compliance", "P1", "reply_and_escalate"),
        (ANSWERED, pattern("rising"), "low", None, "product", "P1", "reply_and_escalate"),
        (
            ANSWERED,
            pattern("recurring", 9),
            "low",
            "exit_load",
            "support_l2_payments",
            "P2",
            "reply_and_escalate",
        ),
        (
            NOT_COVERED_FACTS,
            pattern("recurring", 11, "App crashes"),
            "low",
            None,
            "product",
            "P2",
            "reply_and_close",
        ),
        (
            NOT_COVERED_FACTS,
            pattern("one_off"),
            "low",
            None,
            "support_l2_payments",
            "P2",
            "reply_and_close",
        ),
        (ANSWERED, pattern("one_off"), "low", None, "support_l1", "P3", "reply_and_close"),
    ],
)
def test_routing_rules(facts, pat, risk, fee, team, priority, action) -> None:  # type: ignore[no-untyped-def]
    from app.agents.compliance import ComplianceResult

    note = route(facts=facts, pattern=pat,
                 compliance=ComplianceResult(risk_level=risk, notes="n"), fee_type=fee)  # fmt: skip
    assert (note.team, note.priority, note.action) == (team, priority, action)


def test_disagreement_is_stated_when_facts_answer_a_recurring_issue() -> None:
    note = route(facts=ANSWERED, pattern=pattern("recurring", 40), compliance=None,
                 fee_type="exit_load")  # fmt: skip
    assert note.disagreement and "Facts answered cleanly" in note.disagreement
    assert "40 similar items" in note.disagreement


# --- refinement feedback --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("feedback", "kind"),
    [
        ("make it shorter", "length_tone"),
        ("friendlier tone please", "length_tone"),
        ("wrong scheme, they meant the liquid fund", "different_scheme"),
        ("this is about HDFC Flexi Cap Fund", "different_scheme"),
        ("check if this is the fee issue from last week", "past_theme"),
        ("are many users reporting this?", "past_theme"),
    ],
)
def test_feedback_rules(feedback: str, kind: str) -> None:
    assert feedback_by_rules(feedback) == kind


async def test_free_text_feedback_goes_to_the_model_then_falls_back() -> None:
    llm = ScriptedLLM({"refine_classify": [json.dumps({"type": "past_theme"}),
                                           json.dumps({"type": "nonsense"})]})  # fmt: skip
    assert await classify_feedback(llm, "hmm not quite") == "past_theme"
    assert await classify_feedback(llm, "hmm not quite") == "other"
    assert AGENTS_FOR["other"] == () and AGENTS_FOR["different_scheme"] == ("facts",)


# --- drafting -------------------------------------------------------------------------------


def _ctx(citation: SourceCitation | None) -> PolicyContext:
    index = InMemoryCorpusIndex({citation.chunk_id: citation.url} if citation else {})
    return PolicyContext(("sebi.gov.in", "hdfcfund.com", "amfiindia.com"), TODAY, index)


@pytest.mark.parametrize(
    ("body", "source_type", "cited"),
    [
        (ADVICE_REFUSAL, SourceType.REFUSAL, True),
        (RETURNS_REFUSAL, SourceType.REFUSAL, True),
        (NOT_COVERED, SourceType.NOT_COVERED, False),
        (COMPLAINT_NOT_COVERED, SourceType.NOT_COVERED, False),
        (clarification(None), SourceType.CLARIFICATION, False),
        (clarification("hdfc_liquid"), SourceType.CLARIFICATION, False),
    ],
)
def test_every_template_passes_the_policy_engine(body: str, source_type: SourceType,
                                                 cited: bool) -> None:  # fmt: skip
    citation = CITATION if cited else None
    draft = Draft(body, source_type, citation, ROUTING, model_written=False)
    failed = [r for r in run_policy(draft.artifact(), _ctx(citation)) if not r.passed]
    assert failed == []


def test_code_attaches_source_and_last_checked_lines() -> None:
    draft = Draft("Exit load is 0.0070% on day 1.", SourceType.CORPUS, CITATION, ROUTING, True)
    assert draft.text.splitlines() == ["Exit load is 0.0070% on day 1.",
                                       "Source: https://www.sebi.gov.in/faq.pdf",
                                       "Last checked: 2026-11-17"]  # fmt: skip


@pytest.mark.parametrize(
    "reply",
    [
        "The exit load is 2% on day 1.",  # 2 is not in the Facts answer
        "Exit load is 0.0070% on day 1, see https://example.com.",
    ],  # a URL
)
async def test_model_wording_with_new_numbers_or_urls_falls_back_to_the_facts_answer(
    reply: str,
) -> None:
    llm = ScriptedLLM({"orchestrate": [json.dumps({"reply": reply})]})
    body, fallback = await write_answer(llm, question="q", facts=ANSWERED, passage_text="",
                                        intent="factual")  # fmt: skip
    assert fallback and body == ANSWERED.answer


async def test_feedback_and_rework_reach_the_drafting_prompt() -> None:
    llm = ScriptedLLM({"orchestrate": [json.dumps({"reply": "Exit load is 0.0070% on day 1."})]})
    await write_answer(llm, question="q", facts=ANSWERED, passage_text="", intent="factual",
                       feedback="shorter", rework="- advice_guard: matched")  # fmt: skip
    prompt = llm.calls[0][1][-1]["content"]
    assert "shorter" in prompt and "advice_guard" in prompt
