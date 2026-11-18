"""The loop connector against real Postgres (architecture section 9), with a scripted model and
the hash embedder. Skipped when no database is reachable.

Response loop -> weekly loop: 5 similar questions in 7 days surface their cluster, and the next
weekly run reports it with its count, window and queue item ids.
Weekly loop -> response loop: an approved promote_canonical makes the explainer the answer to
related questions; a newer approved explainer for the same fee type replaces it.
"""

import json
import uuid
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.connector.clusters import SURFACE_MIN_ITEMS, surfaced_for_run
from app.corpus.embed import HashEmbedder
from app.db.models import (
    Approval,
    CanonicalExplainer,
    CorpusChunk,
    CorpusSource,
    DraftVersion,
    FeeExplainer,
    Message,
    PendingAction,
    Pulse,
    QuestionCluster,
    QueueItem,
    Review,
    Theme,
    WeeklyRun,
)
from app.gate.actions import new_pending_action
from app.gate.service import approve_action
from app.mcp.mapping import queue_doc_text
from app.orchestrator.desk import (
    DeskDeps,
    approve_item,
    intake_message,
    latest_draft,
    process_message,
    refine_item,
)
from app.policy import SourceType
from app.weekly.pipeline import WeeklyDeps, execute_run, window_for
from tests.agents.test_desk_db import Retriever, make_llm, user_id
from tests.conftest import active_canonical_ids, restore_canonicals
from tests.weekly.conftest import (
    EXIT_LOAD_CHUNKS,
    FIXTURE_CSV,
    FakeRetriever,
    ScriptedLLM,
    explainer_handler,
    first_quotes_handler,
    pulse_handler,
    theme_label_handler,
)

pytestmark = pytest.mark.db
NOW = datetime.now(UTC)
TODAY = NOW.date()
QUESTION = "Why was exit load deducted when I redeemed my mutual fund units?"

SEBI_URL = "https://www.sebi.gov.in/test-canonical-faq.pdf"
HDFC_URL = "https://files.hdfcfund.com/test-canonical-kim.pdf"
SEBI_TEXT = (
    "An exit load is a fee charged when units are redeemed before a set period. "
    "The exit load is deducted from the redemption amount, so the investor receives less."
)
HDFC_TEXT = "HDFC Liquid Fund exit load: 0.0070% if redeemed on Day 1. Nil from Day 7."
OLD_BULLETS = [
    "An exit load is a fee charged when units are redeemed before a set period.",
    "The exit load is deducted from the redemption amount, so the investor receives less.",
    "HDFC Liquid Fund charges an exit load of 0.0070% if units are redeemed on day 1.",
]
NEW_BULLETS = [
    "Exit load is deducted from the redemption amount when units are redeemed early.",
    "HDFC Liquid Fund has no exit load from day 7.",
]


class NoMcp:
    """promote_canonical never calls a tool."""

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("promote_canonical must not call an MCP tool")

    async def list_tools(self) -> list[dict[str, Any]]:
        return []


@dataclass
class World:
    sessions: async_sessionmaker[AsyncSession]
    messages: list[uuid.UUID] = field(default_factory=list)
    runs: list[uuid.UUID] = field(default_factory=list)
    sources: list[uuid.UUID] = field(default_factory=list)
    chunks: dict[str, uuid.UUID] = field(default_factory=dict)


@pytest.fixture
async def world() -> AsyncIterator[World]:
    engine: AsyncEngine = create_async_engine(Settings().database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"database not reachable: {type(exc).__name__}")
    w = World(async_sessionmaker(engine, expire_on_commit=False))
    async with w.sessions() as s:
        active = await active_canonical_ids(s)
    yield w
    async with w.sessions() as s:
        items = (
            await s.scalars(select(QueueItem).where(QueueItem.message_id.in_(w.messages)))
        ).all()
        clusters = {i.cluster_id for i in items if i.cluster_id}
        # Leftovers of an earlier interrupted run of these tests: test sources and the
        # explainers that cite them.
        leftover_runs = (
            await s.scalars(
                select(FeeExplainer.run_id).where(
                    FeeExplainer.source_urls.contains([SEBI_URL])  # type: ignore[arg-type]
                )
            )
        ).all()
        runs = list({*w.runs, *leftover_runs})
        explainers = (
            await s.scalars(select(FeeExplainer.id).where(FeeExplainer.run_id.in_(runs)))
        ).all()
        origins = [*runs, *(i.id for i in items)]
        actions = (
            await s.scalars(select(PendingAction.id).where(PendingAction.origin_id.in_(origins)))
        ).all()
        await s.execute(delete(Approval).where(Approval.action_id.in_(actions)))
        await s.execute(delete(PendingAction).where(PendingAction.id.in_(actions)))
        await s.execute(delete(Message).where(Message.id.in_(w.messages)))
        await s.execute(delete(Theme).where(Theme.cluster_id.in_(clusters)))
        await s.execute(delete(QuestionCluster).where(QuestionCluster.id.in_(clusters)))
        await s.execute(
            delete(CanonicalExplainer).where(CanonicalExplainer.explainer_id.in_(explainers))
        )
        await s.execute(delete(WeeklyRun).where(WeeklyRun.id.in_(runs)))
        await s.execute(delete(CorpusSource).where(CorpusSource.key.like("test:canonical-%")))
        await s.execute(delete(Review).where(Review.review_id.like("fx:%")))
        await restore_canonicals(s, active)
        await s.commit()
    await engine.dispose()


def desk_deps(w: World, llm: ScriptedLLM) -> DeskDeps:
    settings = Settings(
        _env_file=None,
        pattern_similarity=0.99,  # type: ignore[call-arg]
        theme_match_similarity=0.99,
        question_cluster_similarity=0.9,
    )
    return DeskDeps(
        sessions=w.sessions,
        llm=llm,
        embedder=HashEmbedder(),
        retriever_for=lambda _s: Retriever(),
        settings=settings,
        now=lambda: NOW,
    )  # type: ignore[arg-type]


def fee_canonical_intent(messages: Sequence[Mapping[str, str]]) -> str:
    return json.dumps({"intent": "fee_canonical", "confidence": 0.9})


async def intake(w: World, deps: DeskDeps, message: str) -> list[uuid.UUID]:
    async with w.sessions() as s:
        result = await intake_message(s, deps, message, channel="text", session_id=None)
        await s.commit()
    w.messages.append(result.message_id)
    return result.item_ids


# --- response loop -> weekly loop -----------------------------------------------------------


async def test_a_cluster_surfaces_at_five_items_in_seven_days(world: World) -> None:
    deps = desk_deps(world, make_llm())
    ids = [(await intake(world, deps, QUESTION))[0] for _ in range(SURFACE_MIN_ITEMS - 1)]
    async with world.sessions() as s:
        items = (await s.scalars(select(QueueItem).where(QueueItem.id.in_(ids)))).all()
        [cluster_id] = {i.cluster_id for i in items}
        cluster = await s.get(QuestionCluster, cluster_id)
        assert cluster is not None and not cluster.surfaced  # 4 is not enough

    [fifth] = await intake(world, deps, QUESTION)
    async with world.sessions() as s:
        cluster = await s.get(QuestionCluster, cluster_id)
        item = await s.get(QueueItem, fifth)
        assert item is not None and item.cluster_id == cluster_id
        assert cluster is not None and cluster.surfaced and cluster.surfaced_at == NOW


async def test_items_older_than_seven_days_do_not_count(world: World) -> None:
    deps = desk_deps(world, make_llm())
    ids = [(await intake(world, deps, QUESTION))[0] for _ in range(SURFACE_MIN_ITEMS - 1)]
    async with world.sessions() as s:
        # Four of them are from last month.
        await s.execute(
            update(QueueItem)
            .where(QueueItem.id.in_(ids))
            .values(created_at=NOW - timedelta(days=30))
        )
        await s.commit()
    [fifth] = await intake(world, deps, QUESTION)
    async with world.sessions() as s:
        item = await s.get(QueueItem, fifth)
        assert item is not None
        cluster = await s.get(QuestionCluster, item.cluster_id)
        assert cluster is not None and not cluster.surfaced


def weekly_deps(w: World, llm: ScriptedLLM) -> WeeklyDeps:
    return WeeklyDeps(
        sessions=w.sessions,
        llm=llm,
        embedder=HashEmbedder(),
        retriever_for=lambda _s: FakeRetriever(EXIT_LOAD_CHUNKS),
        settings=Settings(_env_file=None, weekly_resume_on_startup=False),  # type: ignore[call-arg]
        csv_path=FIXTURE_CSV,
        now=lambda: NOW,
    )


def weekly_llm() -> ScriptedLLM:
    return ScriptedLLM(
        {
            "theme_label": theme_label_handler,
            "quote_pick": first_quotes_handler,
            "fee_explainer": explainer_handler,
            "weekly_pulse": pulse_handler,
        }
    )


async def weekly_run(w: World, window_end: date) -> uuid.UUID:
    start, end = window_for(window_end, 12)
    async with w.sessions() as s:
        run = WeeklyRun(window_start=start, window_end=end, triggered_by="eval",
                        status="pending", stage_log={})  # fmt: skip
        s.add(run)
        await s.commit()
    w.runs.append(run.id)
    return run.id


async def test_a_surfaced_cluster_appears_in_the_next_pulse(world: World) -> None:
    deps = desk_deps(world, make_llm())
    ids = [(await intake(world, deps, QUESTION))[0] for _ in range(SURFACE_MIN_ITEMS)]

    run_id = await weekly_run(world, TODAY)
    llm = weekly_llm()
    assert await execute_run(run_id, weekly_deps(world, llm)) == "awaiting_approval"

    async with world.sessions() as s:
        run = await s.get(WeeklyRun, run_id)
        assert run is not None
        item = await s.get(QueueItem, ids[0])
        assert item is not None
        theme = await s.scalar(
            select(Theme).where(Theme.run_id == run_id, Theme.cluster_id == item.cluster_id)
        )
        assert theme is not None and theme.from_support_queue
        assert theme.label == "Exit load confusion"  # named from the questions
        assert sorted(theme.question_ids) == sorted(str(i) for i in ids)
        assert theme.count == SURFACE_MIN_ITEMS and theme.review_ids == []
        reported = next(
            t for t in run.stage_log["queue"]["themes"] if t["theme_id"] == str(theme.id)
        )
        assert reported["window"] == {"start": TODAY.isoformat(), "end": TODAY.isoformat()}

        pulse = await s.scalar(select(Pulse).where(Pulse.run_id == run_id))
        assert pulse is not None
        line = (
            f"Exit load confusion ({SURFACE_MIN_ITEMS} questions, {TODAY:%d %b} to {TODAY:%d %b})"
        )
        assert "From the support queue" in pulse.text_redacted and line in pulse.text_redacted
        assert pulse.word_count <= 250

        doc = await s.scalar(
            select(PendingAction).where(
                PendingAction.origin_id == run_id, PendingAction.kind == "doc_append"
            )
        )
        assert doc is not None
        [entry] = [t for t in doc.payload["support_queue"] if t["label"] == theme.label]
        assert entry["count"] == SURFACE_MIN_ITEMS
        assert sorted(entry["question_ids"]) == sorted(str(i) for i in ids)

        # The review themes are unchanged: the top 3 still come from the reviews.
        review_themes = (
            await s.scalars(
                select(Theme).where(Theme.run_id == run_id, Theme.from_support_queue.is_(False))
            )
        ).all()
        assert all(t.review_ids and not t.question_ids for t in review_themes)


async def test_a_reported_cluster_is_not_reported_again_once_quiet(world: World) -> None:
    deps = desk_deps(world, make_llm())
    ids = [(await intake(world, deps, QUESTION))[0] for _ in range(SURFACE_MIN_ITEMS)]
    first = await weekly_run(world, TODAY)
    assert await execute_run(first, weekly_deps(world, weekly_llm())) == "awaiting_approval"
    async with world.sessions() as s:
        item = await s.get(QueueItem, ids[0])
        assert item is not None
        cluster_id = item.cluster_id
        # Two weeks later, with no new questions, the cluster is not reported again ...
        later = await surfaced_for_run(
            s,
            run_id=uuid.uuid4(),
            window_start=TODAY - timedelta(weeks=10),
            window_end=TODAY + timedelta(days=14),
        )
        assert cluster_id not in {c.cluster_id for c in later}
        # ... but a run that has not reported it yet still must.
        unreported = await surfaced_for_run(
            s, run_id=first, window_start=TODAY - timedelta(weeks=10), window_end=TODAY
        )
        assert cluster_id in {c.cluster_id for c in unreported}


# --- weekly loop -> response loop -----------------------------------------------------------


async def seed_corpus(w: World) -> None:
    embedder = HashEmbedder()
    async with w.sessions() as s:
        for key, url, body in (
            ("test:canonical-sebi", SEBI_URL, SEBI_TEXT),
            ("test:canonical-hdfc", HDFC_URL, HDFC_TEXT),
        ):
            source = CorpusSource(
                key=key, url=url, publisher="sebi", doc_type="faq", title=f"Test {key}",
                fetched_at=datetime(2026, 11, 17, tzinfo=UTC),
            )  # fmt: skip
            s.add(source)
            await s.flush()
            chunk = CorpusChunk(source_id=source.id, chunk_index=0, text=body, page=1,
                                embedding=embedder.embed_documents([body])[0])  # fmt: skip
            s.add(chunk)
            await s.flush()
            w.sources.append(source.id)
            w.chunks[url] = chunk.id
        await s.commit()


async def explainer_action(
    w: World, bullets: list[str], chunk_urls: list[str] | None
) -> PendingAction:
    """A weekly run's exit load explainer and its pending promote_canonical action.
    `chunk_urls` None is an explainer from before Phase 6, with no chunk ids stored."""
    async with w.sessions() as s:
        run = WeeklyRun(window_start=TODAY - timedelta(weeks=12), window_end=TODAY,
                        triggered_by="eval", status="awaiting_approval", stage_log={})  # fmt: skip
        s.add(run)
        await s.flush()
        w.runs.append(run.id)
        explainer = FeeExplainer(
            run_id=run.id, fee_type="exit_load", bullets=bullets,
            source_urls=[SEBI_URL, HDFC_URL], last_checked=date(2026, 11, 17),
            evidence_review_ids=[],
            chunk_ids=[str(w.chunks[u]) for u in chunk_urls] if chunk_urls else [],
        )  # fmt: skip
        s.add(explainer)
        await s.flush()
        action = new_pending_action(
            s, kind="promote_canonical", origin="weekly_run", origin_id=run.id,
            target="internal",
            payload={"fee_type": "exit_load", "explainer_id": str(explainer.id),
                     "bullets": bullets, "source_urls": [SEBI_URL, HDFC_URL],
                     "last_checked": "2026-11-17"},
        )  # fmt: skip
        await s.commit()
        return action


async def promote(w: World, action: PendingAction) -> uuid.UUID:
    async with w.sessions() as s:
        done = await approve_action(
            s,
            action.id,
            action.payload_hash,
            approver_id=await user_id(w),  # type: ignore[arg-type]
            client=NoMcp(),
            embedder=HashEmbedder(),
        )
    assert done.status == "executed" and done.result_id
    return uuid.UUID(done.result_id)


async def ask(w: World, deps: DeskDeps, question: str) -> uuid.UUID:
    [item_id] = await intake(w, deps, question)
    await process_message(deps, [item_id], None)
    return item_id


async def canonical_row(w: World, canonical_id: uuid.UUID) -> CanonicalExplainer:
    async with w.sessions() as s:
        row = await s.get(CanonicalExplainer, canonical_id)
        assert row is not None
        return row


async def test_promotion_links_each_bullet_to_its_corpus_chunk(world: World) -> None:
    await seed_corpus(world)
    old = await promote(world, await explainer_action(world, OLD_BULLETS, None))
    row = await canonical_row(world, old)
    assert row.active and len(row.bullet_chunk_ids) == 3
    # The bullet with a figure can only come from the chunk that has that figure.
    assert row.bullet_chunk_ids[2] == str(world.chunks[HDFC_URL])
    assert all(c in {str(v) for v in world.chunks.values()} for c in row.bullet_chunk_ids)


async def test_a_related_question_reuses_the_canonical_explainer(world: World) -> None:
    await seed_corpus(world)
    canonical_id = await promote(world, await explainer_action(world, OLD_BULLETS, None))
    llm = make_llm(intent=fee_canonical_intent)
    deps = desk_deps(world, llm)

    item_id = await ask(world, deps, QUESTION)
    async with world.sessions() as s:
        item = await s.get(QueueItem, item_id)
        draft = await latest_draft(s, item_id)
        assert item is not None and item.intent == "fee_canonical"
        assert item.status == "awaiting_human"
        assert draft is not None and draft.review_status == "passed"
        assert draft.source_type == SourceType.CANONICAL_EXPLAINER
        assert draft.canonical_id == canonical_id
        body = draft.reply_redacted.split("\nSource:")[0]
        used = [b for b in OLD_BULLETS if b in body]
        assert 1 <= len(used) <= 3 and body == " ".join(used)  # the bullets, verbatim
        assert draft.citation is not None
        assert draft.citation["chunk_id"] in {str(v) for v in world.chunks.values()}
        assert f"Source: {draft.citation['url']}" in draft.reply_redacted
        assert draft.routing is not None
        assert draft.routing["reason"] == "Answered with the approved canonical explainer"
    # No model wrote the reply, and the Facts Agent didn't run.
    assert llm.count("facts") == 0 and llm.count("orchestrate") == 0
    assert (await canonical_row(world, canonical_id)).reuse_count == 1

    # A refinement makes a new draft of the same item: still the explainer, not counted again.
    refined = await refine_item(
        deps, item_id, "please make it shorter", user_id=await user_id(world)
    )  # type: ignore[arg-type]
    assert refined.draft.canonical_id == canonical_id
    assert (await canonical_row(world, canonical_id)).reuse_count == 1

    # Approving the item records the explainer in the doc entry.
    await approve_item(deps, item_id, user_id=await user_id(world))  # type: ignore[arg-type]
    async with world.sessions() as s:
        doc = await s.scalar(
            select(PendingAction).where(
                PendingAction.origin_id == item_id, PendingAction.kind == "doc_append"
            )
        )
        assert doc is not None and doc.payload["canonical_explainer_id"] == str(canonical_id)
        assert f"Canonical answer: explainer {canonical_id}" in "\n".join(
            queue_doc_text(doc.payload)
        )


async def test_a_scheme_fact_question_still_uses_the_scheme_documents(world: World) -> None:
    await seed_corpus(world)
    await promote(world, await explainer_action(world, OLD_BULLETS, None))
    llm = make_llm()  # factual
    item_id = await ask(world, desk_deps(world, llm), "What is the exit load of HDFC Liquid Fund?")
    async with world.sessions() as s:
        draft = await latest_draft(s, item_id)
        assert draft is not None and draft.source_type == SourceType.CORPUS
        assert draft.canonical_id is None
    assert llm.count("facts") == 1


async def test_a_newer_explainer_replaces_the_old_one(world: World) -> None:
    await seed_corpus(world)
    old = await promote(world, await explainer_action(world, OLD_BULLETS, None))
    deps = desk_deps(world, make_llm(intent=fee_canonical_intent))
    await ask(world, deps, QUESTION)

    new = await promote(world, await explainer_action(world, NEW_BULLETS, [SEBI_URL, HDFC_URL]))
    old_row, new_row = await canonical_row(world, old), await canonical_row(world, new)
    assert not old_row.active and old_row.archived_at is not None
    assert new_row.active and new_row.bullet_chunk_ids == [
        str(world.chunks[SEBI_URL]),
        str(world.chunks[HDFC_URL]),
    ]

    item_id = await ask(world, deps, QUESTION)
    async with world.sessions() as s:
        draft = await latest_draft(s, item_id)
        assert draft is not None and draft.canonical_id == new
        body = draft.reply_redacted.split("\nSource:")[0]
        assert not any(b in body for b in OLD_BULLETS)
        assert any(b in body for b in NEW_BULLETS)
        drafts = (
            await s.scalars(select(DraftVersion).where(DraftVersion.canonical_id == old))
        ).all()
        assert all(d.item_id != item_id for d in drafts)
    assert (await canonical_row(world, old)).reuse_count == 1
    assert (await canonical_row(world, new)).reuse_count == 1


def test_api_lists_active_explainers_with_reuse_count(world: World) -> None:
    import asyncio

    from fastapi.testclient import TestClient
    from sqlalchemy.pool import NullPool

    from app.config import get_settings
    from app.db.session import get_session
    from app.main import create_app

    loop = asyncio.get_event_loop()
    loop.run_until_complete(seed_corpus(world))
    old = loop.run_until_complete(
        promote(world, loop.run_until_complete(explainer_action(world, OLD_BULLETS, None)))
    )
    new = loop.run_until_complete(
        promote(
            world,
            loop.run_until_complete(explainer_action(world, NEW_BULLETS, [SEBI_URL, HDFC_URL])),
        )
    )

    settings = Settings(_env_file=None, app_env="test", weekly_resume_on_startup=False)  # type: ignore[call-arg]

    async def session_override() -> AsyncIterator[AsyncSession]:
        engine = create_async_engine(Settings().database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                yield s
        finally:
            await engine.dispose()

    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = session_override
    http = TestClient(app)

    active = {e["id"]: e for e in http.get("/explainers/canonical").json()}
    assert str(new) in active and str(old) not in active
    shown = active[str(new)]
    assert shown["label"] == "Exit load on early redemption"
    assert shown["bullets"] == NEW_BULLETS and shown["source_urls"] == [SEBI_URL, HDFC_URL]
    assert shown["reuse_count"] == 0 and shown["approved_by"] == "local-dev"
    everything = {
        e["id"]: e for e in http.get("/explainers/canonical?include_archived=true").json()
    }
    assert everything[str(old)]["active"] is False and everything[str(old)]["archived_at"]
