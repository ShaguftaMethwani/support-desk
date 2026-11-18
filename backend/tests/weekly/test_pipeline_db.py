"""The weekly loop end to end against real Postgres, with a scripted model, the hash embedder
and a fake retriever. Skipped when no database is reachable."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.corpus.embed import HashEmbedder
from app.db.models import FeeExplainer, PendingAction, Pulse, Review, Theme, WeeklyRun
from app.gate.actions import payload_hash
from app.policy import PolicyContext, ThemeArtifact, run_policy
from app.weekly.pipeline import STAGES, WeeklyDeps, execute_run, window_for
from app.weekly.runner import WeeklyRunner
from tests.weekly.conftest import (
    EXIT_LOAD_CHUNKS,
    FIXTURE_CSV,
    NO_FEES_CSV,
    FakeRetriever,
    ScriptedLLM,
    explainer_handler,
    first_quotes_handler,
    pulse_handler,
    theme_label_handler,
)

pytestmark = pytest.mark.db
WINDOW_END = date(2026, 11, 18)


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine: AsyncEngine = create_async_engine(Settings().database_url)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        await engine.dispose()
        pytest.skip(f"database not reachable: {type(exc).__name__}")
    maker = async_sessionmaker(engine, expire_on_commit=False)
    created: list[uuid.UUID] = []
    maker.created_runs = created  # type: ignore[attr-defined]
    yield maker
    async with maker() as s:
        await s.execute(delete(PendingAction).where(PendingAction.origin_id.in_(created)))
        await s.execute(delete(WeeklyRun).where(WeeklyRun.id.in_(created)))
        await s.execute(delete(Review).where(Review.review_id.like("fx:%")))
        await s.commit()
    await engine.dispose()


def make_llm(**overrides: Any) -> ScriptedLLM:
    handlers: dict[str, Any] = {
        "theme_label": theme_label_handler,
        "quote_pick": first_quotes_handler,
        "fee_explainer": explainer_handler,
        "weekly_pulse": pulse_handler,
    }
    handlers.update(overrides)
    return ScriptedLLM(handlers)


def make_deps(
    sessions: async_sessionmaker[AsyncSession], llm: ScriptedLLM, csv: Path = FIXTURE_CSV
) -> WeeklyDeps:
    settings = Settings(_env_file=None, weekly_resume_on_startup=False)  # type: ignore[call-arg]
    return WeeklyDeps(
        sessions=sessions,
        llm=llm,
        embedder=HashEmbedder(),
        retriever_for=lambda _session: FakeRetriever(EXIT_LOAD_CHUNKS),
        settings=settings,
        csv_path=csv,
        now=lambda: datetime(2026, 11, 18, 9, 0, tzinfo=UTC),
    )


async def new_run(sessions: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    start, end = window_for(WINDOW_END, 12)
    async with sessions() as s:
        run = WeeklyRun(window_start=start, window_end=end, triggered_by="eval", status="pending",
                        stage_log={})  # fmt: skip
        s.add(run)
        await s.commit()
    sessions.created_runs.append(run.id)  # type: ignore[attr-defined]
    return run.id


async def test_full_run_reaches_awaiting_approval(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await new_run(sessions)
    llm = make_llm()
    assert await execute_run(run_id, make_deps(sessions, llm)) == "awaiting_approval"

    async with sessions() as s:
        run = await s.get(WeeklyRun, run_id)
        assert run is not None and run.status == "awaiting_approval" and run.finished_at
        assert all(run.stage_log[stage]["status"] == "done" for stage in STAGES)
        ingest = run.stage_log["ingest"]
        # 32 rows: 1 outside the window, 1 exact duplicate.
        assert ingest["in_window"] == 31 and ingest["exact_duplicates"] == 1
        assert ingest["stored"] == 30 and ingest["reviews_with_pii"] == 1
        stored = (await s.scalars(select(Review).where(Review.review_id.like("fx:%")))).all()
        assert {r.review_id for r in stored} == set(ingest["review_ids"])
        assert all("ABCDE1234F" not in r.text_redacted for r in stored)

        # Review themes only: support-queue themes come from whatever questions the shared
        # database holds (tests/connector covers them).
        themes = (
            await s.scalars(
                select(Theme).where(Theme.run_id == run_id, Theme.from_support_queue.is_(False))
            )
        ).all()
        assert run.stage_log["cluster"]["too_short"] == 1  # "nice app"
        ctx = PolicyContext(("sebi.gov.in",), WINDOW_END)
        for t in themes:  # every theme has ids, a count that matches, and the run's window
            assert t.label and t.rank
            artefact = ThemeArtifact(t.label, t.count, tuple(t.review_ids), (),
                                     run.window_start, run.window_end)  # fmt: skip
            assert all(r.passed for r in run_policy(artefact, ctx))

        texts = {r.review_id: r.text_redacted for r in stored}
        quotes = run.stage_log["quotes"]["quotes"]
        assert len(quotes) == 3
        assert all(q["text"] in texts[q["review_id"]] for q in quotes)  # verbatim substrings

        assert run.stage_log["fees"]["fee_type"] == "exit_load"
        explainer = await s.scalar(select(FeeExplainer).where(FeeExplainer.run_id == run_id))
        assert explainer is not None and len(explainer.source_urls) == 2
        assert sorted(explainer.evidence_review_ids) == [f"fx:fees:{i}" for i in range(4)]

        pulse = await s.scalar(select(Pulse).where(Pulse.run_id == run_id))
        assert pulse is not None and pulse.word_count <= 250
        assert pulse.quote_review_ids == [q["review_id"] for q in quotes]

        actions = (await s.scalars(
            select(PendingAction).where(PendingAction.origin_id == run_id)
        )).all()  # fmt: skip
        assert sorted(a.kind for a in actions) == ["doc_append", "email_draft", "promote_canonical"]
        for a in actions:
            assert a.status == "pending" and a.payload_hash == payload_hash(a.payload)
        email = next(a for a in actions if a.kind == "email_draft")
        assert email.payload["to"] is None
        assert email.payload["subject"].startswith("Weekly Product Pulse + Customer Clarification")


async def test_no_fee_fixture_gives_no_explainer(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await new_run(sessions)
    llm = make_llm()
    assert await execute_run(run_id, make_deps(sessions, llm, NO_FEES_CSV)) == "awaiting_approval"
    async with sessions() as s:
        run = await s.get(WeeklyRun, run_id)
        assert run is not None
        assert run.stage_log["fees"]["fee_type"] is None
        assert run.stage_log["explainer"]["skipped"] == "no fee confusion detected"
        assert await s.scalar(select(FeeExplainer).where(FeeExplainer.run_id == run_id)) is None
        pulse = await s.scalar(select(Pulse).where(Pulse.run_id == run_id))
        assert (
            pulse is not None and "Fee confusion: none detected this week." in pulse.text_redacted
        )
        kinds = (await s.scalars(
            select(PendingAction.kind).where(PendingAction.origin_id == run_id)
        )).all()  # fmt: skip
        assert sorted(kinds) == ["doc_append", "email_draft"]
    assert llm.count("fee_explainer") == 0


async def test_interrupted_run_resumes_from_the_last_finished_stage(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await new_run(sessions)
    # The process "dies" during the pulse: CancelledError is not caught as a stage failure.
    crashing = make_llm(weekly_pulse=lambda _m: asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await execute_run(run_id, make_deps(sessions, crashing))
    async with sessions() as s:
        run = await s.get(WeeklyRun, run_id)
        assert run is not None and run.status == "in_progress"
        done_before = {k: v["finished_at"] for k, v in run.stage_log.items()}
        assert set(done_before) == {
            "ingest", "cluster", "label", "queue", "quotes", "fees", "explainer",
        }  # fmt: skip

    healthy = make_llm()
    runner = WeeklyRunner(lambda: make_deps(sessions, healthy))
    assert run_id in await runner.resume_interrupted()
    await asyncio.gather(*runner._tasks)  # noqa: SLF001
    async with sessions() as s:
        run = await s.get(WeeklyRun, run_id)
        assert run is not None and run.status == "awaiting_approval"
        for stage, finished in done_before.items():  # finished stages were not re-run
            assert run.stage_log[stage]["finished_at"] == finished
    assert healthy.count("theme_label") == 0 and healthy.count("weekly_pulse") == 1


async def test_second_policy_failure_marks_the_run_failed(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    run_id = await new_run(sessions)
    advice = json.loads(pulse_handler([]))
    advice["summary"] = "You should invest in HDFC Mid Cap Fund now."
    llm = make_llm(weekly_pulse=[json.dumps(advice), json.dumps(advice)])
    assert await execute_run(run_id, make_deps(sessions, llm)) == "failed"
    async with sessions() as s:
        run = await s.get(WeeklyRun, run_id)
        assert run is not None and run.status == "failed"
        assert run.error is not None and run.error.startswith("pulse: weekly pulse failed")
        assert run.stage_log["pulse"]["rules"][0]["rule"] == "advice_guard"
        assert await s.scalar(select(Pulse).where(Pulse.run_id == run_id)) is None
    assert llm.count("weekly_pulse") == 2  # one regeneration, with the failure named
    assert "advice_guard" in llm.calls[-1][1][-1]["content"]


def test_trigger_endpoint_starts_one_run_at_a_time(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    from sqlalchemy.pool import NullPool

    from app.config import get_settings
    from app.db.session import get_session
    from app.main import create_app
    from app.weekly.runner import get_weekly_runner

    settings = Settings(_env_file=None, weekly_resume_on_startup=False,  # type: ignore[call-arg]
                        weekly_trigger_token="secret-token")  # fmt: skip
    started: list[uuid.UUID] = []

    class RecordingRunner:
        async def run(self, run_id: uuid.UUID) -> None:
            started.append(run_id)

    async def session_override() -> AsyncIterator[AsyncSession]:
        # TestClient runs requests on its own event loop: no pooled connections shared with
        # the test's loop.
        engine = create_async_engine(Settings().database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                yield s
        finally:
            await engine.dispose()

    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_weekly_runner] = lambda: RecordingRunner()
    client = TestClient(app)

    assert client.post("/weekly/run").status_code == 401
    assert client.post("/weekly/run", headers={"X-Trigger-Token": "wrong"}).status_code == 401
    auth = {"Authorization": "Bearer secret-token"}
    first = client.post("/weekly/run", headers=auth, json={"window_end": "2026-11-18"})
    assert first.status_code == 202, first.text
    run_id = uuid.UUID(first.json()["run_id"])
    sessions.created_runs.append(run_id)  # type: ignore[attr-defined]
    assert started == [run_id]
    second = client.post("/weekly/run", headers=auth)
    assert second.status_code == 409 and second.json()["run_id"] == str(run_id)

    body = client.get(f"/weekly/{run_id}", headers=auth).json()
    assert body["status"] == "pending" and body["window"]["end"] == "2026-11-18"
    assert set(body["stages"]) == set(STAGES)
    assert client.get(f"/weekly/{uuid.uuid4()}", headers=auth).status_code == 404


def test_trigger_without_configured_token_is_unavailable(settings: Settings) -> None:
    from app.config import get_settings
    from app.main import create_app

    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    assert TestClient(app).post("/weekly/run").status_code == 503
