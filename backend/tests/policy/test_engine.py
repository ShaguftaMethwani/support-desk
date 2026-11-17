from datetime import date

import pytest

from app.config import Settings
from app.policy import (
    Citation,
    ExplainerArtifact,
    InMemoryCorpusIndex,
    PolicyContext,
    PulseArtifact,
    ReplyArtifact,
    SourceType,
    ThemeArtifact,
    failures,
    passed,
    run_policy,
)

TODAY = date(2026, 11, 17)
SID = "https://www.hdfcfund.com/liquid-fund-sid.pdf"
AMFI = "https://www.amfiindia.com/investor-corner/exit-load"
CORPUS = InMemoryCorpusIndex({"c1": SID, "c2": AMFI})


@pytest.fixture
def ctx(settings: Settings) -> PolicyContext:
    return PolicyContext.from_settings(settings, corpus=CORPUS, today=TODAY)


def reply(text: str, **kw: object) -> ReplyArtifact:
    defaults: dict[str, object] = {
        "source_type": SourceType.CORPUS,
        "citation": Citation("c1", SID),
        "last_checked": date(2026, 11, 10),
    }
    defaults.update(kw)
    return ReplyArtifact(text, **defaults)  # type: ignore[arg-type]


def failed_rules(results: list) -> set[str]:  # type: ignore[type-arg]
    return {r.rule for r in failures(results)}


def test_good_reply_passes(ctx: PolicyContext) -> None:
    results = run_policy(reply("The exit load is 0.0070% if redeemed on day 1."), ctx)
    assert passed(results), failures(results)
    assert {r.rule for r in results} == {
        "pii_guard",
        "advice_guard",
        "returns_guard",
        "citation_required",
        "length_caps",
        "last_checked_present",
    }


def test_reply_with_four_sentences_fails_length(ctx: PolicyContext) -> None:
    text = "Exit load applies. It is 0.0070% on day 1. It falls daily. It is nil from day 7."
    assert failed_rules(run_policy(reply(text), ctx)) == {"length_caps"}


def test_reply_without_citation_fails(ctx: PolicyContext) -> None:
    results = run_policy(reply("The exit load is 1%.", citation=None), ctx)
    assert failed_rules(results) == {"citation_required"}


def test_citation_must_exist_in_corpus_and_match(ctx: PolicyContext) -> None:
    unknown = reply("The exit load is 1%.", citation=Citation("missing", SID))
    mismatch = reply("The exit load is 1%.", citation=Citation("c1", AMFI))
    assert failed_rules(run_policy(unknown, ctx)) == {"citation_required"}
    assert failed_rules(run_policy(mismatch, ctx)) == {"citation_required"}


def test_citation_must_be_on_allow_list(ctx: PolicyContext) -> None:
    blog = reply("The exit load is 1%.", citation=Citation("b", "https://someblog.com/x"))
    assert "citation_required" in failed_rules(run_policy(blog, ctx))


def test_links_inside_reply_text_must_be_allowed(ctx: PolicyContext) -> None:
    text = "The exit load is 1%. More at https://someblog.com/exit-load."
    assert "citation_required" in failed_rules(run_policy(reply(text), ctx))


def test_not_covered_reply_needs_no_citation_but_cannot_state_figures(ctx: PolicyContext) -> None:
    ok = reply(
        "This isn't covered by the sources I can search.",
        source_type=SourceType.NOT_COVERED,
        citation=None,
        last_checked=None,
    )
    sneaky = reply(
        "This isn't covered, but the exit load is usually 1%.",
        source_type=SourceType.NOT_COVERED,
        citation=None,
        last_checked=None,
    )
    assert passed(run_policy(ok, ctx))
    assert failed_rules(run_policy(sneaky, ctx)) == {"citation_required"}


def test_refusal_template_passes(ctx: PolicyContext) -> None:
    text = (
        "I can only share facts from official documents, not investment advice. "
        "For help deciding, please consult a SEBI-registered investment adviser."
    )
    refusal = reply(text, source_type=SourceType.REFUSAL, citation=Citation("c2", AMFI))
    assert passed(run_policy(refusal, ctx)), failures(run_policy(refusal, ctx))


def test_pii_in_draft_is_a_hard_fail(ctx: PolicyContext) -> None:
    results = run_policy(reply("We noted your PAN ABCPD1234F. Exit load is 1%."), ctx)
    pii = next(r for r in results if r.rule == "pii_guard")
    assert not pii.passed and pii.detail == "found: pan"
    assert "ABCPD1234F" not in (pii.detail or "")


def test_last_checked_rules(ctx: PolicyContext) -> None:
    missing = reply("Exit load is 1%.", last_checked=None)
    future = reply("Exit load is 1%.", last_checked=date(2027, 1, 1))
    assert failed_rules(run_policy(missing, ctx)) == {"last_checked_present"}
    assert failed_rules(run_policy(future, ctx)) == {"last_checked_present"}


def explainer(
    bullets: tuple[str, ...], sources: tuple[str, ...] = (SID, AMFI)
) -> ExplainerArtifact:
    return ExplainerArtifact(bullets, sources, date(2026, 11, 10))


def test_good_explainer_passes(ctx: PolicyContext) -> None:
    e = explainer(
        (
            "Exit load is a charge on redeeming units before a set period.",
            "For HDFC Liquid Fund it is 0.0070% on day 1 and falls to nil from day 7.",
            "It is deducted from the redemption amount.",
        )
    )
    assert passed(run_policy(e, ctx)), failures(run_policy(e, ctx))


def test_explainer_caps_and_sources(ctx: PolicyContext) -> None:
    seven = explainer(tuple(f"Fact {i}." for i in range(7)))
    one_source = explainer(("Fact.",), (SID,))
    off_corpus = explainer(("Fact.",), (SID, "https://www.sebi.gov.in/not-in-corpus"))
    assert failed_rules(run_policy(seven, ctx)) == {"length_caps"}
    assert failed_rules(run_policy(one_source, ctx)) == {"explainer_sources"}
    assert failed_rules(run_policy(off_corpus, ctx)) == {"citation_required"}


def test_pulse_word_cap(ctx: PolicyContext) -> None:
    assert passed(run_policy(PulseArtifact("Exit load confusion is the top theme. " * 30), ctx))
    assert failed_rules(run_policy(PulseArtifact("word " * 251), ctx)) == {"length_caps"}


def test_pulse_blocks_quoted_return_figures(ctx: PolicyContext) -> None:
    text = 'Top theme: performance. One user wrote: "my returns were 12% last year".'
    assert "returns_guard" in failed_rules(run_policy(PulseArtifact(text), ctx))


@pytest.mark.parametrize(
    ("theme", "ok"),
    [
        (ThemeArtifact("Exit load confusion", 2, ("r1", "r2"), (), TODAY, TODAY), True),
        (ThemeArtifact("Exit load confusion", 3, ("r1", "r2"), ("q1",), TODAY, TODAY), True),
        (ThemeArtifact("No ids", 0, (), (), TODAY, TODAY), False),
        (ThemeArtifact("Wrong count", 5, ("r1",), (), TODAY, TODAY), False),
        (ThemeArtifact("No window", 1, ("r1",)), False),
        (ThemeArtifact("Duplicate", 2, ("r1", "r1"), (), TODAY, TODAY), False),
        (ThemeArtifact("Backwards", 1, ("r1",), (), TODAY, date(2026, 1, 1)), False),
    ],
)
def test_theme_provenance(ctx: PolicyContext, theme: ThemeArtifact, ok: bool) -> None:
    assert passed(run_policy(theme, ctx)) is ok


def test_unknown_artifact_type_is_rejected(ctx: PolicyContext) -> None:
    with pytest.raises(TypeError):
        run_policy("just a string", ctx)  # type: ignore[arg-type]
