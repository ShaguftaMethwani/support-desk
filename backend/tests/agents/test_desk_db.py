"""The response loop against real Postgres, with a scripted model, the hash embedder and a
fake retriever. Skipped when no database is reachable."""

import json
import re
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.corpus.embed import HashEmbedder
from app.corpus.retrieve import RetrievedChunk, SearchTrace
from app.db.models import (
    AgentRun,
    AppUser,
    ChatSession,
    DraftVersion,
    Message,
    PendingAction,
    QuestionCluster,
    QueueItem,
    RefinementEvent,
    Review,
    ReviewResult,
)
from app.intent.session import open_session, remember, snapshot
from app.orchestrator import desk as desk_module
from app.orchestrator.desk import (
    DeskDeps,
    DeskError,
    approve_item,
    edit_draft,
    intake_message,
    latest_draft,
    process_message,
    refine_item,
)
from app.orchestrator.draft import ADVICE_DOC_TYPES, RETURNS_DOC_TYPES, Draft
from app.policy import SourceType
from tests.weekly.conftest import ScriptedLLM, chunk

pytestmark = pytest.mark.db
NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)

LIQUID = chunk(
    "liq1",
    "hdfc_liquid_kim",
    "https://files.hdfcfund.com/kim-liquid.pdf",
    "[HDFC Liquid Fund] Exit load is 0.0070% if redeemed on Day 1. Nil from Day 7.",
    0.86,
)
ELSS = chunk(
    "elss1",
    "amfi_categorisation",
    "https://www.amfiindia.com/categorisation",
    "ELSS schemes have a lock-in period of 3 years.",
    0.84,
)
EDUCATION = chunk(
    "edu1",
    "sebi_mf_faq",
    "https://www.sebi.gov.in/faq.pdf",
    "Investors should understand scheme categories and risk.",
    0.7,
)
FACTSHEET = chunk(
    "fs1",
    "hdfc_factsheet",
    "https://files.hdfcfund.com/factsheet.pdf",
    "[HDFC Large Cap Fund] Fund details.",
    0.7,
)
UNRELATED = chunk("x1", "sebi_mf_faq", "https://www.sebi.gov.in/faq.pdf", "Unrelated text.", 0.3)
UPI_REVIEWS = [
    "UPI payment failed and the money was debited from my bank account",
    "my UPI payment failed but the bank account was debited",
    "payment failed through UPI and money debited from bank account, no refund",
    "UPI payment failed again, amount debited from my bank account",
    "money debited from bank account but UPI payment shows failed",
    "UPI payment failed, my bank account was debited twice",
]


class Retriever:
    async def search(
        self,
        query: str,
        *,
        scheme: str | None = None,
        include_platform: bool = False,
        k: int = 4,
        doc_types: Sequence[str] | None = None,
    ) -> tuple[list[RetrievedChunk], SearchTrace]:
        if doc_types == ADVICE_DOC_TYPES:
            found = [EDUCATION]
        elif doc_types == RETURNS_DOC_TYPES:
            found = [FACTSHEET]
        elif "exit load" in query.lower():
            found = [LIQUID]
        elif "lock" in query.lower():
            found = [ELSS]
        else:
            found = [UNRELATED]
        return found[:k], SearchTrace(query, scheme, include_platform)


def intent_handler(messages: Sequence[Mapping[str, str]]) -> str:
    q = messages[-1]["content"].lower()
    label = "complaint" if any(w in q for w in ("debited", "late", "crash")) else "factual"
    return json.dumps({"intent": label, "confidence": 0.9})


def facts_handler(messages: Sequence[Mapping[str, str]]) -> str:
    passage = messages[-1]["content"].split("[1]", 1)[1].split("\n", 1)[1].split("\n\n")[0]
    return json.dumps({"not_covered": False, "answer": passage.strip(), "passage": 1})


def orchestrate_handler(messages: Sequence[Mapping[str, str]]) -> str:
    facts = re.search(r"Facts: (.*)", messages[-1]["content"])
    return json.dumps({"reply": facts.group(1) if facts else "No facts."})


def make_llm(**overrides: Any) -> ScriptedLLM:
    handlers: dict[str, Any] = {
        "intent": intent_handler,
        "facts": facts_handler,
        "orchestrate": orchestrate_handler,
        "review_judge": lambda _m: json.dumps({"reads_as_advice": False}),
        "compliance_check": lambda _m: json.dumps({"advice_seeking": True}),
        "refine_classify": lambda _m: json.dumps({"type": "other"}),
        "split": lambda _m: json.dumps({"parts": []}),
    }
    handlers.update(overrides)
    return ScriptedLLM(handlers)


@dataclass
class Tracker:
    sessions: async_sessionmaker[AsyncSession]
    messages: list[uuid.UUID] = field(default_factory=list)
    chats: list[uuid.UUID] = field(default_factory=list)


@pytest.fixture
async def tracker() -> AsyncIterator[Tracker]:
    engine: AsyncEngine = create_async_engine(Settings().database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"database not reachable: {type(exc).__name__}")
    t = Tracker(async_sessionmaker(engine, expire_on_commit=False))
    yield t
    async with t.sessions() as s:
        items = (
            await s.scalars(select(QueueItem).where(QueueItem.message_id.in_(t.messages)))
        ).all()
        clusters = {i.cluster_id for i in items if i.cluster_id}
        await s.execute(
            delete(PendingAction).where(PendingAction.origin_id.in_([i.id for i in items]))
        )
        await s.execute(delete(Message).where(Message.id.in_(t.messages)))
        await s.execute(delete(QuestionCluster).where(QuestionCluster.id.in_(clusters)))
        await s.execute(delete(ChatSession).where(ChatSession.id.in_(t.chats)))
        await s.execute(delete(Review).where(Review.review_id.like("dsk:%")))
        await s.commit()
    await engine.dispose()


def make_deps(t: Tracker, llm: ScriptedLLM) -> DeskDeps:
    settings = Settings(
        _env_file=None,
        pattern_similarity=0.5,  # type: ignore[call-arg]
        theme_match_similarity=0.99,
        question_cluster_similarity=0.9,
    )
    return DeskDeps(
        sessions=t.sessions,
        llm=llm,
        embedder=HashEmbedder(),
        retriever_for=lambda _s: Retriever(),
        settings=settings,
        now=lambda: NOW,
    )  # type: ignore[arg-type]


async def ask(
    t: Tracker,
    deps: DeskDeps,
    message: str,
    *,
    chat: ChatSession | None = None,
) -> list[uuid.UUID]:
    async with t.sessions() as s:
        intake = await intake_message(
            s, deps, message, channel="text", session_id=chat.id if chat else None
        )
        await s.commit()
    t.messages.append(intake.message_id)
    await process_message(deps, intake.item_ids, snapshot(chat) if chat else None)
    return intake.item_ids


async def user_id(t: Tracker) -> uuid.UUID:
    async with t.sessions() as s:
        user = await s.scalar(select(AppUser).where(AppUser.username == "local-dev"))
        if user is None:
            user = AppUser(username="local-dev", password_hash="!", role="agent")
            s.add(user)
            await s.commit()
        return user.id


async def item_state(t: Tracker, item_id: uuid.UUID) -> dict[str, Any]:
    async with t.sessions() as s:
        item = await s.get(QueueItem, item_id)
        assert item is not None
        draft = await latest_draft(s, item_id)
        runs = (await s.scalars(select(AgentRun).where(AgentRun.item_id == item_id))).all()
        reviews = (
            await s.scalars(
                select(ReviewResult)
                .join(DraftVersion)
                .where(DraftVersion.item_id == item_id)
                .order_by(ReviewResult.attempt)
            )
        ).all()
        return {
            "item": item,
            "draft": draft,
            "runs": {r.agent: r for r in runs},
            "reviews": reviews,
        }


async def seed_upi_reviews(t: Tracker) -> None:
    vectors = HashEmbedder().embed_documents(UPI_REVIEWS)
    async with t.sessions() as s:
        for i, (body, vec) in enumerate(zip(UPI_REVIEWS, vectors, strict=True)):
            await s.execute(
                insert(Review)
                .values(
                    review_id=f"dsk:upi:{i}",
                    source="play_store",
                    date=(NOW - timedelta(days=3 + i * 3)).date(),
                    rating=1,
                    text_redacted=body,
                    pii_flags=[],
                    embedding=vec,
                )
                .on_conflict_do_nothing()
            )
        await s.commit()


# --- branch tests ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "agents", "source_type", "team", "priority", "check"),
    [
        (
            "What is the exit load of HDFC Liquid Fund?",
            {"facts", "pattern", "compliance"},
            "corpus",
            "support_l1",
            "P3",
            "clean",
        ),
        (
            "What is the favourite colour of the HDFC Liquid Fund manager?",
            {"facts", "pattern", "compliance"},
            "not_covered",
            "support_l2_payments",
            "P2",
            None,
        ),
        (
            "UPI payment failed and the money was debited from my bank account",
            {"facts", "pattern", "compliance"},
            "not_covered",
            "product",
            "P2",
            "recurring",
        ),
        (
            "My PAN is ABCDE1234F, why is my redemption late?",
            {"facts", "pattern", "compliance"},
            "not_covered",
            "compliance",
            "P1",
            "pii",
        ),
        (
            "Should I buy HDFC Mid Cap Fund?",
            {"pattern", "compliance"},
            "refusal",
            "support_l1",
            "P3",
            "advice",
        ),
        (
            "What returns did HDFC Large Cap Fund give last year?",
            {"pattern", "compliance"},
            "refusal",
            "support_l1",
            "P3",
            "returns",
        ),
    ],
)
async def test_branches(
    tracker: Tracker,
    message: str,
    agents: set[str],
    source_type: str,
    team: str,
    priority: str,
    check: str | None,
) -> None:
    if check == "recurring":
        await seed_upi_reviews(tracker)
    deps = make_deps(tracker, make_llm())
    [item_id] = await ask(tracker, deps, message)
    st = await item_state(tracker, item_id)
    draft = st["draft"]
    assert set(st["runs"]) == agents  # which agents fired
    assert all(r.status == "ok" for r in st["runs"].values())
    assert draft.source_type == source_type and draft.review_status == "passed"
    assert (draft.routing["team"], draft.routing["priority"]) == (team, priority)
    assert st["item"].status == "awaiting_human"
    pattern = st["runs"]["pattern"].output_json
    compliance = st["runs"]["compliance"].output_json
    if check == "clean":
        assert draft.reply_redacted.startswith("[HDFC Liquid Fund] Exit load is 0.0070%")
        assert "Source: https://files.hdfcfund.com/kim-liquid.pdf" in draft.reply_redacted
        assert "Last checked: 2026-11-17" in draft.reply_redacted
        runs = st["runs"].values()  # the three agents overlap in time
        assert max(r.started_at for r in runs) <= min(r.finished_at for r in runs)
    if check == "recurring":
        assert pattern["label"] == "recurring" and pattern["review_count"] >= 5
        assert {f"dsk:upi:{i}" for i in range(5)} <= set(pattern["evidence_ids"])
    if check == "pii":
        assert "ABCDE1234F" not in st["item"].text_redacted
        assert "[PAN_REDACTED]" in st["item"].text_redacted
        assert compliance["risk_level"] == "high"
        rules = {r["rule"]: r["passed"] for r in st["reviews"][-1].rule_results}
        assert rules["pii_guard"] is True  # the reviewer checked the draft for PII too
    if check in ("advice", "returns"):
        assert st["item"].intent == check and st["item"].shared_slots["intent_method"] == "pre_rule"
        url = EDUCATION.url if check == "advice" else FACTSHEET.url
        assert f"Source: {url}" in draft.reply_redacted


async def test_mixed_message_answers_facts_and_refuses_advice(tracker: Tracker) -> None:
    deps = make_deps(tracker, make_llm())
    ids = await ask(
        tracker,
        deps,
        "What is the exit load of HDFC Liquid Fund, what is the "
        "ELSS lock-in, and should I redeem now?",
    )
    states = [await item_state(tracker, i) for i in ids]
    assert [s["draft"].source_type for s in states] == ["corpus", "corpus", "refusal"]
    assert [s["draft"].citation["url"] for s in states] == [LIQUID.url, ELSS.url, EDUCATION.url]
    assert all(s["draft"].review_status == "passed" for s in states)


async def test_session_refusal_holds_when_advice_is_rephrased(tracker: Tracker) -> None:
    deps = make_deps(tracker, make_llm())
    uid = await user_id(tracker)
    async with tracker.sessions() as s:
        chat = await open_session(s, None, uid, ttl=timedelta(hours=1), now=NOW)
        await s.commit()
    tracker.chats.append(chat.id)
    [first] = await ask(tracker, deps, "Should I invest in HDFC Mid Cap Fund?", chat=chat)
    async with tracker.sessions() as s:
        chat = await open_session(s, chat.id, uid, ttl=timedelta(hours=1), now=NOW)
        remember(chat, schemes=["hdfc_mid_cap"], advice_refused=True)
        await s.commit()
    [second] = await ask(tracker, deps, "Is putting money in mid cap a smart move?", chat=chat)
    st = await item_state(tracker, second)
    assert st["item"].intent == "advice"
    assert st["item"].shared_slots["intent_method"] == "sticky_refusal"
    assert st["draft"].source_type == "refusal"


# --- reviewer rework ------------------------------------------------------------------------


async def test_uncited_draft_is_sent_back_once_then_blocked(
    tracker: Tracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str | None] = []

    async def uncited(
        deps: Any,
        s: Any,
        *,
        question: str,
        cls: Any,
        results: Any,
        feedback: str | None = None,
        rework: str | None = None,
    ) -> Draft:
        calls.append(rework)
        from app.orchestrator.routing import RoutingNote

        note = RoutingNote(team="support_l1", priority="P3", reason="r", action="reply_and_close")
        return Draft("The exit load is 0.0070% on day 1.", SourceType.CORPUS, None, note, True)

    monkeypatch.setattr(desk_module, "build_draft", uncited)
    deps = make_deps(tracker, make_llm())
    [item_id] = await ask(tracker, deps, "What is the exit load of HDFC Liquid Fund?")
    assert calls[0] is None and calls[1] is not None and "citation_required" in calls[1]
    st = await item_state(tracker, item_id)
    assert st["draft"].review_status == "blocked" and st["item"].status == "blocked"
    assert [r.attempt for r in st["reviews"]] == [1, 2]
    assert not any(r.passed for r in st["reviews"])
    with pytest.raises(DeskError, match="passed review"):
        await approve_item(deps, item_id, user_id=await user_id(tracker))


async def test_a_blocked_draft_can_be_fixed_by_editing_then_approved(tracker: Tracker) -> None:
    """Editing is the way out of a blocked draft. (A draft blocked for a missing citation
    stays blocked: an edit changes the words, not the source.)"""
    advice = json.dumps({"reply": "You should invest in it now."})
    deps = make_deps(tracker, make_llm(orchestrate=[advice, advice]))
    uid = await user_id(tracker)
    [item_id] = await ask(tracker, deps, "What is the exit load of HDFC Liquid Fund?")
    assert (await item_state(tracker, item_id))["draft"].review_status == "blocked"
    still_bad = await edit_draft(deps, item_id, "You should buy more units.", user_id=uid)
    assert still_bad.review_status == "blocked"
    fixed = await edit_draft(deps, item_id, "Exit load is 0.0070% on Day 1.", user_id=uid)
    assert fixed.review_status == "passed" and fixed.citation is not None
    assert len(await approve_item(deps, item_id, user_id=uid)) == 2


async def test_advice_wording_is_reworked_with_the_named_failure(tracker: Tracker) -> None:
    llm = make_llm(
        orchestrate=[
            json.dumps({"reply": "You should invest in it now."}),
            json.dumps({"reply": "Exit load is 0.0070% on Day 1."}),
        ]
    )
    deps = make_deps(tracker, llm)
    [item_id] = await ask(tracker, deps, "What is the exit load of HDFC Liquid Fund?")
    st = await item_state(tracker, item_id)
    assert st["draft"].review_status == "passed" and st["draft"].version == 2
    assert "advice_guard" in [c for c in llm.calls if c[0] == "orchestrate"][1][1][-1]["content"]


# --- human actions --------------------------------------------------------------------------


async def test_pii_in_edits_and_refinement_notes_is_redacted(tracker: Tracker) -> None:
    llm = make_llm()
    deps = make_deps(tracker, llm)
    uid = await user_id(tracker)
    [item_id] = await ask(tracker, deps, "What is the exit load of HDFC Liquid Fund?")
    edited = await edit_draft(
        deps, item_id, "Exit load is 0.0070% on Day 1. PAN ABCDE1234F.", user_id=uid
    )
    assert "ABCDE1234F" not in edited.reply_redacted and "[PAN_REDACTED]" in edited.reply_redacted
    assert edited.author == "human"
    await refine_item(deps, item_id, "make it shorter, my PAN is ABCDE1234F", user_id=uid)
    prompts = [m[-1]["content"] for task, m in llm.calls if task == "orchestrate"]
    assert "ABCDE1234F" not in prompts[-1] and "[PAN_REDACTED]" in prompts[-1]


async def test_refinement_fires_only_affected_agents_and_caps_at_five(tracker: Tracker) -> None:
    deps = make_deps(tracker, make_llm())
    uid = await user_id(tracker)
    [item_id] = await ask(tracker, deps, "What is the exit load of HDFC Liquid Fund?")
    expected = [
        ("make it shorter", "length_tone", ["orchestrator", "reviewer"]),
        (
            "wrong scheme, they meant HDFC Liquid Fund",
            "different_scheme",
            ["facts", "orchestrator", "reviewer"],
        ),
        (
            "is this the fee issue from last week?",
            "past_theme",
            ["pattern", "orchestrator", "reviewer"],
        ),
        ("friendlier tone", "length_tone", ["orchestrator", "reviewer"]),
        ("hmm", "other", ["orchestrator", "reviewer"]),
    ]
    for round_no, (feedback, kind, fired) in enumerate(expected, start=1):
        out = await refine_item(deps, item_id, feedback, user_id=uid)
        assert (out.round, out.feedback_type, out.agents_fired) == (round_no, kind, fired)
    with pytest.raises(DeskError, match="capped at 5"):
        await refine_item(deps, item_id, "shorter again", user_id=uid)
    async with tracker.sessions() as s:
        events = (
            await s.scalars(select(RefinementEvent).where(RefinementEvent.item_id == item_id))
        ).all()
        runs = (await s.scalars(select(AgentRun).where(AgentRun.item_id == item_id))).all()
    assert len(events) == 5
    # 3 initial runs, plus facts once and pattern once from refinement.
    assert sorted(r.agent for r in runs) == ["compliance", "facts", "facts", "pattern", "pattern"]


async def test_approve_creates_two_pending_actions_and_writes_nothing(tracker: Tracker) -> None:
    deps = make_deps(tracker, make_llm())
    [item_id] = await ask(tracker, deps, "What is the exit load of HDFC Liquid Fund?")
    ids = await approve_item(deps, item_id, user_id=await user_id(tracker))
    async with tracker.sessions() as s:
        actions = (
            await s.scalars(
                select(PendingAction).where(PendingAction.id.in_([uuid.UUID(i) for i in ids]))
            )
        ).all()
        item = await s.get(QueueItem, item_id)
    assert sorted(a.kind for a in actions) == ["doc_append", "email_draft"]
    assert all(a.status == "pending" for a in actions)
    assert item is not None and item.status == "approved"
    with pytest.raises(DeskError, match="already approved"):
        await approve_item(deps, item_id, user_id=await user_id(tracker))


def test_api_refuses_to_approve_a_blocked_draft(tracker: Tracker) -> None:
    """The server, not the UI, refuses: a blocked draft returns 409 from the approve endpoint."""
    import asyncio

    from fastapi.testclient import TestClient
    from sqlalchemy.pool import NullPool

    from app.config import get_settings
    from app.db.session import get_session
    from app.main import create_app
    from app.orchestrator.deps import get_desk_deps

    settings = Settings(
        _env_file=None,
        weekly_resume_on_startup=False,  # type: ignore[call-arg]
        app_env="test",
    )

    async def make_blocked() -> uuid.UUID:
        async with tracker.sessions() as s:
            message = Message(channel="text", text_redacted="blocked test", split_method="single")
            s.add(message)
            await s.flush()
            item = QueueItem(
                message_id=message.id, part_index=0, text_redacted="blocked test", status="blocked"
            )
            s.add(item)
            await s.flush()
            s.add(
                DraftVersion(
                    item_id=item.id,
                    version=1,
                    reply_redacted="x",
                    source_type="corpus",
                    author="system",
                    review_status="blocked",
                )
            )
            await s.commit()
            tracker.messages.append(message.id)
            return item.id

    item_id = asyncio.get_event_loop().run_until_complete(make_blocked())

    async def session_override() -> AsyncIterator[AsyncSession]:
        engine = create_async_engine(Settings().database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                yield s
        finally:
            await engine.dispose()

    no_pool = async_sessionmaker(
        create_async_engine(Settings().database_url, poolclass=NullPool), expire_on_commit=False
    )
    deps = DeskDeps(
        sessions=no_pool,
        llm=make_llm(),
        embedder=HashEmbedder(),
        retriever_for=lambda _s: Retriever(),
        settings=settings,
    )  # type: ignore[arg-type]
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_desk_deps] = lambda: deps
    client = TestClient(app)
    response = client.post(f"/queue/{item_id}/approve")
    assert response.status_code == 409 and "passed review" in response.json()["detail"]
    assert client.post(f"/queue/{uuid.uuid4()}/approve").status_code == 404


def test_desk_endpoints_refuse_outside_local(tracker: Tracker) -> None:
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app

    settings = Settings(
        _env_file=None,
        weekly_resume_on_startup=False,  # type: ignore[call-arg]
        app_env="production",
    )
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    assert client.post("/chat/message", json={"text": "hi"}).status_code == 401
    assert client.get("/queue").status_code == 401
