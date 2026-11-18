"""The approval gate against real Postgres, with a fake MCP client. Skipped when no database
is reachable."""

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import date
from typing import Any

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.corpus.embed import HashEmbedder
from app.db.models import (
    Approval,
    AppUser,
    CanonicalExplainer,
    DraftVersion,
    FeeExplainer,
    Message,
    PendingAction,
    QueueItem,
    WeeklyRun,
)
from app.gate.actions import new_pending_action
from app.gate.service import GateError, approve_action, combined_reply, reject_action, retry_action
from app.mcp.client import McpToolError
from app.mcp.execute import NotApprovedError, execute
from tests.conftest import active_canonical_ids, restore_canonicals

pytestmark = pytest.mark.db


class FakeMcp:
    """Records calls. `fail` raises on the next calls; `delay` keeps a call in flight."""

    def __init__(self, fail: list[McpToolError] | None = None, delay: float = 0.0) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail = list(fail or [])
        self.delay = delay

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail.pop(0)
        if tool == "google_docs_append":
            return {
                "success": True,
                "document_id": arguments["document_id"],
                "revision_id": "r2",
                "skipped": False,
            }
        return {"success": True, "draft_id": f"draft-{len(self.calls)}"}

    async def list_tools(self) -> list[dict[str, Any]]:
        return []


class World:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.runs: list[uuid.UUID] = []
        self.messages: list[uuid.UUID] = []
        self.user_id: uuid.UUID | None = None

    async def user(self) -> uuid.UUID:
        if self.user_id is None:
            async with self.sessions() as s:
                user = await s.scalar(select(AppUser).where(AppUser.username == "local-dev"))
                if user is None:
                    user = AppUser(username="local-dev", password_hash="!", role="agent")
                    s.add(user)
                    await s.commit()
                self.user_id = user.id
        return self.user_id

    async def weekly_actions(self) -> dict[str, PendingAction]:
        """A weekly run with a fee explainer and its three pending actions."""
        async with self.sessions() as s:
            run = WeeklyRun(
                window_start=date(2026, 8, 27),
                window_end=date(2026, 11, 18),
                triggered_by="eval",
                status="awaiting_approval",
                stage_log={},
            )
            s.add(run)
            await s.flush()
            explainer = FeeExplainer(
                run_id=run.id,
                fee_type="exit_load",
                bullets=["Exit load is charged on early redemption."],
                source_urls=["https://www.sebi.gov.in/a", "https://www.amfiindia.com/b"],
                last_checked=date(2026, 11, 17),
                evidence_review_ids=["r1"],
                chunk_ids=[str(uuid.uuid4())],  # promotion needs a chunk per bullet
            )
            s.add(explainer)
            await s.flush()
            doc = {
                "date": "2026-11-18",
                "window": {"start": "2026-08-27", "end": "2026-11-18"},
                "top_themes": [],
                "weekly_pulse": "Pulse text.",
                "identified_fee_issue": "Exit load on early redemption",
                "explanation_bullets": list(explainer.bullets),
                "source_links": list(explainer.source_urls),
                "last_checked": "2026-11-17",
            }
            actions = {
                "doc_append": new_pending_action(
                    s,
                    kind="doc_append",
                    origin="weekly_run",
                    origin_id=run.id,
                    target="doc-xyz-123",
                    payload=doc,
                ),
                "email_draft": new_pending_action(
                    s,
                    kind="email_draft",
                    origin="weekly_run",
                    origin_id=run.id,
                    target="gmail_drafts",
                    payload={"subject": "Weekly", "body": "B", "to": None},
                ),
                "promote_canonical": new_pending_action(
                    s,
                    kind="promote_canonical",
                    origin="weekly_run",
                    origin_id=run.id,
                    target="internal",
                    payload={
                        "fee_type": "exit_load",
                        "explainer_id": str(explainer.id),
                        "bullets": list(explainer.bullets),
                        "source_urls": list(explainer.source_urls),
                        "last_checked": "2026-11-17",
                    },
                ),
            }
            await s.commit()
            self.runs.append(run.id)
            return actions

    async def reload(self, action_id: uuid.UUID) -> PendingAction:
        async with self.sessions() as s:
            a = await s.get(PendingAction, action_id)
            assert a is not None
            return a

    async def approve(
        self,
        action: PendingAction,
        client: FakeMcp,
        shown: str | None = None,
    ) -> PendingAction:
        async with self.sessions() as s:
            return await approve_action(
                s,
                action.id,
                shown or action.payload_hash,
                approver_id=await self.user(),
                client=client,
                embedder=HashEmbedder(),
            )


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
        origins = [*w.runs, *w.messages]
        ids = (
            await s.scalars(select(PendingAction.id).where(PendingAction.origin_id.in_(origins)))
        ).all()
        explainers = (
            await s.scalars(select(FeeExplainer.id).where(FeeExplainer.run_id.in_(w.runs)))
        ).all()
        await s.execute(delete(Approval).where(Approval.action_id.in_(ids)))
        await s.execute(delete(PendingAction).where(PendingAction.id.in_(ids)))
        await s.execute(
            delete(CanonicalExplainer).where(CanonicalExplainer.explainer_id.in_(explainers))
        )
        await s.execute(delete(WeeklyRun).where(WeeklyRun.id.in_(w.runs)))
        await s.execute(delete(Message).where(Message.id.in_(w.messages)))
        await restore_canonicals(s, active)
        await s.commit()
    await engine.dispose()


async def test_execute_without_a_valid_approval_raises(world: World) -> None:
    actions = await world.weekly_actions()
    doc, email = actions["doc_append"], actions["email_draft"]
    client = FakeMcp()
    async with world.sessions() as s:
        with pytest.raises(NotApprovedError):
            await execute(s, doc.id, uuid.uuid4(), client)  # no such approval
        # An approval that belongs to another action doesn't count.
        approval = Approval(
            action_id=email.id, payload_hash=email.payload_hash, approver_id=await world.user()
        )
        s.add(approval)
        await s.flush()
        with pytest.raises(NotApprovedError):
            await execute(s, doc.id, approval.id, client)
        await s.rollback()
    assert client.calls == []


async def test_stale_hash_is_rejected_and_nothing_happens(world: World) -> None:
    doc = (await world.weekly_actions())["doc_append"]
    client = FakeMcp()
    with pytest.raises(GateError, match="payload changed") as info:
        await world.approve(doc, client, shown="0" * 64)
    assert info.value.status == 409
    assert (await world.reload(doc.id)).status == "pending"
    async with world.sessions() as s:
        assert await s.scalar(select(Approval).where(Approval.action_id == doc.id)) is None
    assert client.calls == []


async def test_approving_the_doc_append_leaves_the_email_draft_pending(world: World) -> None:
    actions = await world.weekly_actions()
    client = FakeMcp()
    done = await world.approve(actions["doc_append"], client)
    assert done.status == "executed" and done.result_id == "doc-xyz-123@r2"
    assert [tool for tool, _ in client.calls] == ["google_docs_append"]
    assert (await world.reload(actions["email_draft"].id)).status == "pending"
    assert (await world.reload(actions["promote_canonical"].id)).status == "pending"


async def test_a_double_click_executes_once(world: World) -> None:
    email = (await world.weekly_actions())["email_draft"]
    client = FakeMcp(delay=0.5)  # the first call is still in flight when the second arrives
    results = await asyncio.gather(
        world.approve(email, client), world.approve(email, client), return_exceptions=True
    )
    assert sum(isinstance(r, PendingAction) for r in results) == 1
    errors = [r for r in results if isinstance(r, GateError)]
    assert len(errors) == 1 and errors[0].status == 409
    assert len(client.calls) == 1
    async with world.sessions() as s:
        approvals = (await s.scalars(select(Approval).where(Approval.action_id == email.id))).all()
    assert len(approvals) == 1


async def test_only_promote_canonical_creates_a_canonical_explainer(world: World) -> None:
    actions = await world.weekly_actions()
    explainer_id = uuid.UUID(actions["promote_canonical"].payload["explainer_id"])

    async def canonical() -> list[CanonicalExplainer]:
        async with world.sessions() as s:
            rows = await s.scalars(
                select(CanonicalExplainer).where(CanonicalExplainer.explainer_id == explainer_id)
            )
            return list(rows.all())

    await world.approve(actions["doc_append"], FakeMcp())
    await world.approve(actions["email_draft"], FakeMcp())
    assert await canonical() == []
    client = FakeMcp()
    promoted = await world.approve(actions["promote_canonical"], client)
    assert promoted.status == "executed" and client.calls == []  # no tool for internal actions
    [row] = await canonical()
    assert row.active and row.fee_type == "exit_load" and str(row.id) == promoted.result_id


async def test_a_tool_failure_is_retryable_and_never_writes_twice(world: World) -> None:
    doc = (await world.weekly_actions())["doc_append"]
    client = FakeMcp(fail=[McpToolError("TIMEOUT", "no answer", outcome_unknown=True)])
    failed = await world.approve(doc, client)
    assert failed.status == "failed" and "outcome unknown" in (failed.error or "")
    with pytest.raises(GateError, match="already failed"):
        await world.approve(doc, client)  # approve is one-shot; retry is the way back
    async with world.sessions() as s:
        retried = await retry_action(s, doc.id, client=client, embedder=HashEmbedder())
    assert retried.status == "executed" and retried.error is None
    first, second = client.calls
    # The retry sends the same entry with the same dedupe key, so the server skips it if the
    # timed-out first attempt did reach Google.
    assert first == second and first[1]["dedupe_key"] in first[1]["content"]


async def test_reject_marks_only_that_action(world: World) -> None:
    actions = await world.weekly_actions()
    async with world.sessions() as s:
        rejected = await reject_action(s, actions["email_draft"].id)
    assert rejected.status == "rejected"
    assert (await world.reload(actions["doc_append"].id)).status == "pending"
    with pytest.raises(GateError):
        await world.approve(actions["email_draft"], FakeMcp())


async def test_combined_reply_needs_every_part_decided(world: World) -> None:
    async with world.sessions() as s:
        message = Message(channel="text", text_redacted="two questions", split_method="rules")
        s.add(message)
        await s.flush()
        items = []
        for i, (q, status) in enumerate(
            [("Q one?", "approved"), ("Q two?", "awaiting_human"), ("Q three?", "approved")]
        ):
            item = QueueItem(message_id=message.id, part_index=i, text_redacted=q, status=status)
            s.add(item)
            await s.flush()
            s.add(
                DraftVersion(
                    item_id=item.id,
                    version=1,
                    reply_redacted=f"Answer {i + 1}.",
                    source_type="corpus",
                    author="system",
                    review_status="passed",
                )
            )
            items.append(item)
        await s.commit()
    world.messages.append(message.id)
    async with world.sessions() as s:
        with pytest.raises(GateError, match="not approved or rejected"):
            await combined_reply(s, message.id, today="2026-11-18")
        await s.rollback()
        item = await s.get(QueueItem, items[1].id)
        assert item is not None
        item.status = "rejected"
        await s.commit()
        action = await combined_reply(s, message.id, today="2026-11-18")
    assert (
        action.kind == "email_draft" and action.origin == "message" and action.status == "pending"
    )
    assert action.payload["body"] == "1. Q one?\nAnswer 1.\n\n2. Q three?\nAnswer 3."
    assert action.payload["to"] is None
    async with world.sessions() as s:
        with pytest.raises(GateError, match="already has a combined reply"):
            await combined_reply(s, message.id, today="2026-11-18")


def test_api_shows_the_payload_and_needs_its_hash(world: World) -> None:
    from fastapi.testclient import TestClient
    from sqlalchemy.pool import NullPool

    from app.api.actions import get_embedder, get_mcp_client
    from app.config import get_settings
    from app.db.session import get_session
    from app.main import create_app

    actions = asyncio.get_event_loop().run_until_complete(world.weekly_actions())
    doc = actions["doc_append"]
    settings = Settings(_env_file=None, weekly_resume_on_startup=False, app_env="test")  # type: ignore[call-arg]

    async def session_override() -> AsyncIterator[AsyncSession]:
        engine = create_async_engine(Settings().database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                yield s
        finally:
            await engine.dispose()

    client_double = FakeMcp()
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_mcp_client] = lambda: client_double
    app.dependency_overrides[get_embedder] = lambda: HashEmbedder()
    http = TestClient(app)

    listed = {a["id"]: a for a in http.get("/actions/pending").json()["actions"]}
    shown = listed[str(doc.id)]
    assert shown["payload"] == doc.payload and shown["target"] == "doc-xyz-123"
    assert (
        http.post(f"/actions/{doc.id}/approve", json={"payload_hash": "f" * 64}).status_code == 409
    )
    ok = http.post(f"/actions/{doc.id}/approve", json={"payload_hash": shown["payload_hash"]})
    assert ok.status_code == 200 and ok.json()["status"] == "executed"
    assert (
        http.post(
            f"/actions/{doc.id}/approve", json={"payload_hash": shown["payload_hash"]}
        ).status_code
        == 409
    )
    assert len(client_double.calls) == 1
