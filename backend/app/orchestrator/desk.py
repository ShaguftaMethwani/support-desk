"""The response loop (architecture section 7.1): intake, agents, drafting, review, human actions.

intake_message   guard -> splitter -> one message row and one queue item per part, each with
                 its embedding and rolling question cluster
process_item     classify -> canonical explainer lookup -> run the agents the route needs, in
                 parallel (one agent_run row each, with start and finish times) -> draft ->
                 review (one rework) -> store
refine_item      re-run only the agents the feedback affects, reuse the others' last outputs
edit_draft       a human's text, guarded, as a new version, re-validated
approve_item     only a passed draft; creates its doc append and email draft pending actions
reject_item      changes only that item's status

Every human text (messages, feedback, edits) passes through the ingress guard before it is
stored or sent to a model. Model failures leave the item "unavailable", never half-drafted.
"""

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agents.compliance import ComplianceResult, pii_types, run_compliance
from app.agents.facts import FactsAgent, FactsResult, SourceCitation
from app.agents.pattern import PatternResult, PatternSettings, run_pattern
from app.config import Settings
from app.connector.canonical import CanonicalAnswer, canonical_for_question, count_reuse
from app.connector.clusters import assign_cluster
from app.corpus.embed import Embedder
from app.corpus.schemes import BY_SLUG
from app.db.models import (
    AgentRun,
    DraftVersion,
    Message,
    QueueItem,
    RefinementEvent,
    ReviewResult,
)
from app.gate.actions import new_pending_action
from app.guard import guard
from app.intent.classifier import Classification, classify
from app.intent.rules import find_scheme
from app.intent.session import SessionState
from app.intent.splitter import Part, split_message
from app.llm import LLMError, fallback_models_used, track_fallback
from app.observability import get_logger
from app.orchestrator.draft import (
    ADVICE_REFUSAL,
    COMPLAINT_NOT_COVERED,
    RETURNS_REFUSAL,
    Completer,
    Draft,
    Retriever,
    clarification,
    not_covered_reply,
    refusal_source,
    write_answer,
)
from app.orchestrator.refine import AGENTS_FOR, classify_feedback
from app.orchestrator.routing import RoutingNote, route
from app.policy import InMemoryCorpusIndex, PolicyContext, RuleResult, SourceType, failures
from app.reviewer.review import review_reply
from app.weekly.fees import BY_KEY as FEE_TYPES

log = get_logger(__name__)

AGENTS = ("facts", "pattern", "compliance")
NO_FACTS_INTENTS = frozenset({"advice", "returns", "unclear"})
MAX_REFINEMENTS = 5


class DeskError(Exception):
    """A refused human action, with the HTTP status the API should return."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass
class DeskDeps:
    sessions: async_sessionmaker[AsyncSession]
    llm: Completer
    embedder: Embedder
    retriever_for: Callable[[AsyncSession], Retriever]
    settings: Settings
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @property
    def pattern_settings(self) -> PatternSettings:
        s = self.settings
        return PatternSettings(
            s.pattern_similarity,
            s.theme_match_similarity,
            s.pattern_window_days,
            s.recurring_min,
            s.rising_min,
        )


# --- intake ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Intake:
    message_id: uuid.UUID
    item_ids: list[uuid.UUID]
    dropped: list[str]
    split_method: str
    pii_flags: list[str]


async def intake_message(
    s: AsyncSession,
    deps: DeskDeps,
    raw_text: str,
    *,
    channel: str,
    session_id: uuid.UUID | None,
) -> Intake:
    clean = guard(raw_text)
    # A confirmed voice transcript was redacted before the user saw it: its tokens still count.
    pii_flags = sorted(set(clean.pii_flags) | set(pii_types(clean.text)))
    split = await split_message(clean.text, deps.llm)
    message = Message(
        session_id=session_id,
        channel=channel,
        text_redacted=clean.text,
        pii_flags=pii_flags,
        split_method=split.method,
        parts_dropped=split.dropped,
    )
    s.add(message)
    await s.flush()
    embeddings = await asyncio.to_thread(
        deps.embedder.embed_documents, [p.text for p in split.parts]
    )
    ids = []
    for index, (part, vector) in enumerate(zip(split.parts, embeddings, strict=True)):
        item = QueueItem(
            message_id=message.id,
            part_index=index,
            text_redacted=part.text,
            shared_slots=part.slots,
            status="new",
            embedding=vector,
        )
        s.add(item)
        await s.flush()
        await assign_cluster(
            s,
            item,
            vector,
            threshold=deps.settings.question_cluster_similarity,
            active_days=7,
            now=deps.now(),
        )
        ids.append(item.id)
    return Intake(message.id, ids, split.dropped, split.method, pii_flags)


# --- agents ---------------------------------------------------------------------------------


@dataclass
class AgentOutcome:
    agent: str
    status: str  # ok | error | cached
    output: BaseModel | None
    started_at: datetime
    finished_at: datetime
    error: str | None = None

    @property
    def latency_ms(self) -> int:
        return int((self.finished_at - self.started_at).total_seconds() * 1000)


async def _timed(agent: str, work: Callable[[], Awaitable[BaseModel]]) -> AgentOutcome:
    started = datetime.now(UTC)
    try:
        output = await work()
        return AgentOutcome(agent, "ok", output, started, datetime.now(UTC))
    except Exception as exc:  # noqa: BLE001 - recorded on the agent_run row
        return AgentOutcome(
            agent, "error", None, started, datetime.now(UTC), f"{type(exc).__name__}: {exc}"[:300]
        )


def facts_query(text: str, scheme: str | None, fee_type: str | None) -> str:
    """The part's own words, plus the shared scheme or fee it inherited, so "and its
    riskometer?" searches for the right scheme."""
    query = text
    if scheme and find_scheme(text) != scheme:
        query += f" ({BY_SLUG[scheme].name})"
    if fee_type and not FEE_TYPES[fee_type].matches(text):
        query += f" ({FEE_TYPES[fee_type].label})"
    return query


async def run_agents(
    deps: DeskDeps,
    item: QueueItem,
    cls: Classification,
    agents: Sequence[str],
) -> dict[str, AgentOutcome]:
    """Run the given agents concurrently. Each gets its own database session and only the
    redacted question and slots, never another agent's output."""
    text = item.text_redacted
    embedding = list(item.embedding) if item.embedding is not None else []

    async def facts() -> BaseModel:
        async with deps.sessions() as s:
            agent = FactsAgent(
                deps.retriever_for(s),
                deps.llm,  # type: ignore[arg-type]
                min_similarity=deps.settings.retrieval_min_similarity,
            )
            return await agent.run(facts_query(text, cls.scheme, cls.fee_type), scheme=cls.scheme)

    async def pattern() -> BaseModel:
        async with deps.sessions() as s:
            return await run_pattern(
                s,
                embedding,
                item_id=item.id,
                cluster_id=item.cluster_id,
                now=deps.now(),
                cfg=deps.pattern_settings,
            )

    async def compliance() -> BaseModel:
        return await run_compliance(text, deps.llm)

    work = {"facts": facts, "pattern": pattern, "compliance": compliance}
    outcomes = await asyncio.gather(*(_timed(name, work[name]) for name in agents))
    return {o.agent: o for o in outcomes}


# --- drafting and review --------------------------------------------------------------------


@dataclass
class Results:
    facts: FactsResult | None = None
    pattern: PatternResult | None = None
    compliance: ComplianceResult | None = None
    canonical: CanonicalAnswer | None = None  # an approved explainer that answers the question


def _results(outcomes: dict[str, AgentOutcome], canonical: CanonicalAnswer | None) -> Results:
    r = Results(canonical=canonical)
    for name, o in outcomes.items():
        if o.output is not None:
            setattr(r, name, o.output)
    return r


async def find_canonical_answer(
    deps: DeskDeps, s: AsyncSession, item: QueueItem, cls: Classification
) -> CanonicalAnswer | None:
    """The canonical explainer reply for this item, if one applies (architecture 9.2)."""
    if item.embedding is None:
        return None
    return await canonical_for_question(
        s,
        deps.embedder,
        embedding=list(item.embedding),
        intent=cls.intent,
        scheme=cls.scheme,
        fee_type=cls.fee_type,
        settings=deps.settings,
    )


def agents_for(cls: Classification, canonical: CanonicalAnswer | None) -> list[str]:
    """Facts is skipped for refusals and clarifications, and when a canonical explainer
    answers the question (route table in architecture section 7.3)."""
    skip_facts = cls.intent in NO_FACTS_INTENTS or canonical is not None
    return [a for a in AGENTS if not (a == "facts" and skip_facts)]


async def build_draft(
    deps: DeskDeps,
    s: AsyncSession,
    *,
    question: str,
    cls: Classification,
    results: Results,
    feedback: str | None = None,
    rework: str | None = None,
) -> Draft:
    canonical = results.canonical
    routing = route(
        facts=results.facts,
        pattern=results.pattern,
        compliance=results.compliance,
        fee_type=cls.fee_type,
        canonical=canonical is not None and cls.intent not in NO_FACTS_INTENTS,
    )
    if cls.intent in ("advice", "returns"):
        source = await refusal_source(deps.retriever_for(s), question, cls.intent, cls.scheme)
        body = ADVICE_REFUSAL if cls.intent == "advice" else RETURNS_REFUSAL
        return Draft(body, SourceType.REFUSAL, source, routing, model_written=False)
    if cls.intent == "unclear":
        return Draft(clarification(cls.scheme), SourceType.CLARIFICATION, None, routing, False)
    if canonical is not None:
        # Code builds the reply from the explainer's own bullets; no model rewrites it.
        return Draft(
            canonical.body,
            SourceType.CANONICAL_EXPLAINER,
            canonical.citation,
            routing,
            model_written=False,
            canonical_id=canonical.canonical_id,
        )
    facts = results.facts
    if facts is None or facts.status != "answered" or facts.answer is None:
        body = COMPLAINT_NOT_COVERED if cls.intent == "complaint" else not_covered_reply(facts)
        return Draft(body, SourceType.NOT_COVERED, None, routing, model_written=False)
    body, fallback = await write_answer(
        deps.llm,
        question=question,
        facts=facts,
        passage_text="",
        intent=cls.intent,
        feedback=feedback,
        rework=rework,
    )
    # A fee_canonical question with no canonical explainer is answered from the corpus.
    return Draft(
        body, SourceType.CORPUS, facts.citation, routing, model_written=True, fallback_used=fallback
    )


def _ctx(deps: DeskDeps, draft: Draft) -> PolicyContext:
    index = InMemoryCorpusIndex(
        {draft.citation.chunk_id: draft.citation.url} if draft.citation else {}
    )
    return PolicyContext(tuple(deps.settings.source_domains), deps.now().date(), index)


def describe(results: list[RuleResult]) -> str:
    return "\n".join(f"- {r.rule}: {r.detail}" for r in results)


@dataclass
class Reviewed:
    drafts: list[tuple[Draft, list[RuleResult]]]  # each attempt with its review
    status: str  # passed | blocked


async def draft_and_review(
    deps: DeskDeps,
    s: AsyncSession,
    *,
    question: str,
    cls: Classification,
    results: Results,
    feedback: str | None = None,
) -> Reviewed:
    """Draft, review, and on a failure send the named failure back once. A second failure
    blocks the draft. Template drafts get no rework: rewording a template can't fix it."""
    draft = await build_draft(
        deps, s, question=question, cls=cls, results=results, feedback=feedback
    )
    review = await review_reply(
        draft.artifact(), _ctx(deps, draft), llm=deps.llm, use_judge=draft.model_written
    )
    if not failures(review):
        return Reviewed([(draft, review)], "passed")
    if not draft.model_written:
        return Reviewed([(draft, review)], "blocked")
    second = await build_draft(
        deps,
        s,
        question=question,
        cls=cls,
        results=results,
        feedback=feedback,
        rework=describe(failures(review)),
    )
    review2 = await review_reply(
        second.artifact(), _ctx(deps, second), llm=deps.llm, use_judge=True
    )
    status = "passed" if not failures(review2) else "blocked"
    return Reviewed([(draft, review), (second, review2)], status)


async def _next_version(s: AsyncSession, item_id: uuid.UUID) -> int:
    current = await s.scalar(
        select(func.max(DraftVersion.version)).where(DraftVersion.item_id == item_id)
    )
    return int(current or 0) + 1


async def store_review(
    s: AsyncSession,
    item: QueueItem,
    reviewed: Reviewed,
    *,
    author: str = "system",
    author_id: uuid.UUID | None = None,
) -> DraftVersion:
    version = await _next_version(s, item.id)
    used_before = set(
        (
            await s.scalars(
                select(DraftVersion.canonical_id).where(
                    DraftVersion.item_id == item.id, DraftVersion.canonical_id.is_not(None)
                )
            )
        ).all()
    )
    final: DraftVersion | None = None
    for attempt, (draft, results) in enumerate(reviewed.drafts, start=1):
        last = attempt == len(reviewed.drafts)
        row = DraftVersion(
            item_id=item.id,
            version=version,
            reply_redacted=draft.text,
            citation=draft.citation.model_dump(mode="json") if draft.citation else None,
            last_checked=draft.last_checked,
            source_type=draft.source_type.value,
            canonical_id=draft.canonical_id,
            routing=draft.routing.model_dump(mode="json"),
            author=author,
            author_id=author_id,
            # "fallback_model": a model ran out of daily quota and the other one answered.
            # "fallback": the model's wording failed code checks; the Facts answer was used.
            model_tag=(
                "fallback_model"
                if fallback_models_used()
                else ("fallback" if draft.fallback_used else None)
            ),
            review_status=reviewed.status if last else "rework",
        )
        s.add(row)
        await s.flush()
        s.add(
            ReviewResult(
                draft_version_id=row.id,
                attempt=attempt,
                passed=not failures(results),
                rule_results=[
                    {"rule": r.rule, "passed": r.passed, "detail": r.detail} for r in results
                ],
            )
        )
        version += 1
        final = row
    assert final is not None
    # Reuse counts queue items, so a refined or edited draft doesn't count twice.
    if final.canonical_id is not None and final.canonical_id not in used_before:
        await count_reuse(s, final.canonical_id)
    item.status = "awaiting_human" if reviewed.status == "passed" else "blocked"
    return final


def _store_runs(
    s: AsyncSession, item_id: uuid.UUID, outcomes: dict[str, AgentOutcome], version_id: uuid.UUID
) -> None:
    for o in outcomes.values():
        s.add(
            AgentRun(
                item_id=item_id,
                draft_version_id=version_id,
                agent=o.agent,
                status=o.status,
                output_json=(
                    o.output.model_dump(mode="json") if o.output is not None else {"error": o.error}
                ),
                started_at=o.started_at,
                finished_at=o.finished_at,
                latency_ms=o.latency_ms,
            )
        )


# --- processing one item --------------------------------------------------------------------


@dataclass(frozen=True)
class ItemOutcome:
    item_id: uuid.UUID
    status: str
    intent: str | None
    scheme: str | None
    advice_refused: bool


async def process_item(
    deps: DeskDeps, item_id: uuid.UUID, state: SessionState | None
) -> ItemOutcome:
    with track_fallback():  # a fallback model anywhere in this item tags its draft
        return await _process_item(deps, item_id, state)


async def _process_item(
    deps: DeskDeps, item_id: uuid.UUID, state: SessionState | None
) -> ItemOutcome:
    """Classify, run the agents, draft, review and store. No database session is held while
    the agents run: they open their own, and one message can have up to five parts."""
    marks = [("start", time.monotonic())]
    async with deps.sessions() as s:
        item = await s.get(QueueItem, item_id)
        assert item is not None
        try:
            cls = await classify(
                deps.llm,
                _part(item),
                session_scheme=state.last_scheme if state else None,
                advice_refused=state.advice_refused if state else False,
                min_confidence=deps.settings.intent_min_confidence,
                sticky_min_confidence=deps.settings.sticky_advice_min_confidence,
            )
        except LLMError:
            item.status = "unavailable"
            await s.commit()
            return ItemOutcome(item.id, "unavailable", None, None, False)
        item.intent, item.intent_confidence = cls.intent, cls.confidence
        item.shared_slots = {
            **item.shared_slots,
            "scheme": cls.scheme,
            "scheme_source": cls.scheme_source,
            "fee_type": cls.fee_type,
            "intent_method": cls.method,
            "intent_rule": cls.rule,
        }
        item.status = "drafting"
        canonical = await find_canonical_answer(deps, s, item, cls)
        await s.commit()
    marks.append(("classify", time.monotonic()))

    outcomes = await run_agents(deps, item, cls, agents_for(cls, canonical))
    marks.append(("agents", time.monotonic()))

    async with deps.sessions() as s:
        item = await s.get(QueueItem, item_id, populate_existing=True)
        assert item is not None
        if any(o.status == "error" for o in outcomes.values()):
            return await _unavailable(s, item, outcomes, cls)
        try:
            reviewed = await draft_and_review(
                deps,
                s,
                question=item.text_redacted,
                cls=cls,
                results=_results(outcomes, canonical),
            )
        except LLMError:
            return await _unavailable(s, item, outcomes, cls)
        marks.append(("draft_review", time.monotonic()))
        final = await store_review(s, item, reviewed)
        _store_runs(s, item.id, outcomes, final.id)
        await s.commit()
    marks.append(("store", time.monotonic()))
    timings = {name: int((t - marks[i][1]) * 1000) for i, (name, t) in enumerate(marks[1:])}
    log.info("desk_item_done", item_id=str(item.id), intent=cls.intent, status=item.status,
             **{f"{k}_ms": v for k, v in timings.items()})  # fmt: skip
    return ItemOutcome(item.id, item.status, cls.intent, cls.scheme, cls.intent == "advice")


async def _unavailable(
    s: AsyncSession, item: QueueItem, outcomes: dict[str, AgentOutcome], cls: Classification
) -> ItemOutcome:
    item.status = "unavailable"
    _store_runs_without_version(s, item.id, outcomes)
    await s.commit()
    return ItemOutcome(item.id, "unavailable", cls.intent, cls.scheme, False)


def _store_runs_without_version(
    s: AsyncSession, item_id: uuid.UUID, outcomes: dict[str, AgentOutcome]
) -> None:
    for o in outcomes.values():
        s.add(
            AgentRun(
                item_id=item_id,
                agent=o.agent,
                status=o.status,
                output_json=(
                    o.output.model_dump(mode="json") if o.output is not None else {"error": o.error}
                ),
                started_at=o.started_at,
                finished_at=o.finished_at,
                latency_ms=o.latency_ms,
            )
        )


def _part(item: QueueItem) -> Part:
    slots = item.shared_slots or {}
    return Part(
        item.text_redacted,
        slots.get("scheme"),
        slots.get("fee_type"),
        bool(slots.get("scheme_inherited")),
        bool(slots.get("fee_inherited")),
    )


def _classification(item: QueueItem) -> Classification:
    slots = item.shared_slots or {}
    return Classification(
        item.intent or "factual",
        item.intent_confidence or 0.0,
        slots.get("intent_method", "model"),
        slots.get("intent_rule"),
        slots.get("scheme"),
        slots.get("scheme_source"),
        slots.get("fee_type"),
    )


# --- human actions --------------------------------------------------------------------------


async def latest_draft(s: AsyncSession, item_id: uuid.UUID) -> DraftVersion | None:
    return await s.scalar(
        select(DraftVersion)
        .where(DraftVersion.item_id == item_id)
        .order_by(DraftVersion.version.desc())
        .limit(1)
    )


async def _load_item(s: AsyncSession, item_id: uuid.UUID, *, lock: bool = True) -> QueueItem:
    stmt = select(QueueItem).where(QueueItem.id == item_id)
    item = await s.scalar(stmt.with_for_update() if lock else stmt)
    if item is None:
        raise DeskError(404, "queue item not found")
    return item


def _require_open(item: QueueItem) -> None:
    if item.status in ("approved", "rejected"):
        raise DeskError(409, f"item is already {item.status}")
    if item.status in ("new", "drafting"):
        raise DeskError(409, "item is still being drafted")


_RESULT_TYPES: dict[str, type[BaseModel]] = {
    "facts": FactsResult,
    "pattern": PatternResult,
    "compliance": ComplianceResult,
}


async def cached_outputs(s: AsyncSession, item_id: uuid.UUID) -> dict[str, AgentOutcome]:
    """The last successful output of each agent for this item."""
    rows = (
        await s.scalars(
            select(AgentRun)
            .where(AgentRun.item_id == item_id, AgentRun.status == "ok")
            .order_by(AgentRun.started_at.desc())
        )
    ).all()
    out: dict[str, AgentOutcome] = {}
    for row in rows:
        if row.agent in out or row.agent not in _RESULT_TYPES or row.output_json is None:
            continue
        model = _RESULT_TYPES[row.agent].model_validate(row.output_json)
        out[row.agent] = AgentOutcome(
            row.agent, "cached", model, row.started_at, row.finished_at or row.started_at
        )
    return out


@dataclass(frozen=True)
class RefineOutcome:
    round: int
    feedback_type: str
    agents_fired: list[str]
    draft: DraftVersion


async def refine_item(
    deps: DeskDeps, item_id: uuid.UUID, feedback_raw: str, *, user_id: uuid.UUID
) -> RefineOutcome:
    with track_fallback():
        return await _refine_item(deps, item_id, feedback_raw, user_id=user_id)


async def _refine_item(
    deps: DeskDeps, item_id: uuid.UUID, feedback_raw: str, *, user_id: uuid.UUID
) -> RefineOutcome:
    feedback = guard(feedback_raw).text  # never stored or sent to a model unredacted
    async with deps.sessions() as s:
        item = await _load_item(s, item_id)
        _require_open(item)
        rounds = await s.scalar(
            select(func.count())
            .select_from(RefinementEvent)
            .where(RefinementEvent.item_id == item.id)
        )
        round_no = int(rounds or 0) + 1
        if round_no > MAX_REFINEMENTS:
            raise DeskError(409, f"refinement is capped at {MAX_REFINEMENTS} rounds")
        kind = await classify_feedback(deps.llm, feedback)
        cls = _classification(item)
        if kind == "different_scheme":
            new_scheme = find_scheme(feedback)
            if new_scheme:
                cls = Classification(
                    cls.intent,
                    cls.confidence,
                    cls.method,
                    cls.rule,
                    new_scheme,
                    "refinement",
                    cls.fee_type,
                )
                item.shared_slots = {
                    **item.shared_slots,
                    "scheme": new_scheme,
                    "scheme_source": "refinement",
                }
        # Looked up again: a newer explainer may have replaced the one the last draft used.
        canonical = await find_canonical_answer(deps, s, item, cls)
        allowed = agents_for(cls, canonical)
        cached = await cached_outputs(s, item.id)
        # Agents with no earlier output also run, e.g. Facts after the canonical explainer
        # that answered this item was archived with no replacement.
        rerun = [a for a in allowed if a in AGENTS_FOR[kind] or a not in cached]
        fresh = await run_agents(deps, item, cls, rerun) if rerun else {}
        if any(o.status == "error" for o in fresh.values()):
            raise DeskError(503, "an agent is temporarily unavailable; try again")
        merged = {**cached, **fresh}
        try:
            reviewed = await draft_and_review(
                deps,
                s,
                question=item.text_redacted,
                cls=cls,
                results=_results(merged, canonical),
                feedback=feedback,
            )
        except LLMError as exc:
            raise DeskError(503, "the model is temporarily unavailable; try again") from exc
        final = await store_review(s, item, reviewed)
        _store_runs(s, item.id, fresh, final.id)
        fired = [*rerun, "orchestrator", "reviewer"]
        s.add(
            RefinementEvent(item_id=item.id, round=round_no, feedback_type=kind, agents_fired=fired)
        )
        await s.commit()
        return RefineOutcome(round_no, kind, fired, final)


async def edit_draft(
    deps: DeskDeps, item_id: uuid.UUID, body_raw: str, *, user_id: uuid.UUID
) -> DraftVersion:
    """A human's reply text as a new version. The citation and Last checked lines are kept
    from the current draft and re-attached by code. The policy engine re-runs; an edit that
    fails it is stored as blocked and can't be approved."""
    body = guard(body_raw).text
    async with deps.sessions() as s:
        item = await _load_item(s, item_id)
        _require_open(item)
        current = await latest_draft(s, item.id)
        if current is None:
            raise DeskError(409, "item has no draft to edit")
        citation = SourceCitation.model_validate(current.citation) if current.citation else None
        draft = Draft(
            body,
            SourceType(current.source_type),
            citation,
            RoutingNote.model_validate(current.routing),
            model_written=False,
            canonical_id=current.canonical_id,
        )
        results = await review_reply(draft.artifact(), _ctx(deps, draft), llm=None, use_judge=False)
        status = "passed" if not failures(results) else "blocked"
        final = await store_review(
            s, item, Reviewed([(draft, results)], status), author="human", author_id=user_id
        )
        await s.commit()
        return final


async def approve_item(deps: DeskDeps, item_id: uuid.UUID, *, user_id: uuid.UUID) -> list[str]:
    """Accept the latest draft and create its two pending actions. Writes nothing to Google.
    Refused unless the latest draft passed review: a blocked draft can't be approved here,
    whatever the UI shows."""
    async with deps.sessions() as s:
        item = await _load_item(s, item_id)
        _require_open(item)
        draft = await latest_draft(s, item.id)
        if draft is None or draft.review_status != "passed":
            raise DeskError(409, "only a draft that passed review can be approved")
        today = deps.now().date().isoformat()
        doc = {
            "date": today,
            "question": item.text_redacted,
            "reply": draft.reply_redacted,
            "citation": draft.citation,
            "routing": draft.routing,
            "draft_version": draft.version,
        }
        if draft.canonical_id is not None:
            doc["canonical_explainer_id"] = str(draft.canonical_id)
        email = {
            "subject": f"Reply to customer question — {today}",
            "body": draft.reply_redacted,
            "to": None,
        }
        doc_target = deps.settings.google_doc_id or "google_doc_unset"
        actions = [
            new_pending_action(s, kind="doc_append", origin="queue_item", origin_id=item.id,
                               target=doc_target, payload=doc),
            new_pending_action(s, kind="email_draft", origin="queue_item", origin_id=item.id,
                               target="gmail_drafts", payload=email),
        ]  # fmt: skip
        await s.flush()
        ids = [str(a.id) for a in actions]
        item.status = "approved"
        await s.commit()
        return ids


async def reject_item(deps: DeskDeps, item_id: uuid.UUID) -> None:
    async with deps.sessions() as s:
        item = await _load_item(s, item_id)
        if item.status in ("approved", "rejected"):
            raise DeskError(409, f"item is already {item.status}")
        item.status = "rejected"
        await s.commit()


# --- a whole message ------------------------------------------------------------------------


async def process_message(
    deps: DeskDeps, item_ids: list[uuid.UUID], state: SessionState | None
) -> list[ItemOutcome]:
    """Parts run in parallel, all against the same session snapshot, at most
    `desk_parallel_items` at a time; the rest queue rather than fail."""
    limit = asyncio.Semaphore(deps.settings.desk_parallel_items)

    async def one(item_id: uuid.UUID) -> ItemOutcome:
        async with limit:
            return await process_item(deps, item_id, state)

    return list(await asyncio.gather(*(one(i) for i in item_ids)))


def session_ttl(settings: Settings) -> timedelta:
    return timedelta(minutes=settings.chat_session_ttl_minutes)
