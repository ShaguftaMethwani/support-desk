"""The weekly loop (architecture section 8.1), from trigger to "awaiting approval".

Stages run in order. Each one commits its own output and records itself in
`weekly_run.stage_log`, so an interrupted run resumes from the first stage not marked done.
Every stage can be re-run safely: it replaces whatever an interrupted attempt left behind.

    ingest     load the CSV, guard, window, dedupe, store reviews with embeddings
    cluster    PCA + KMeans over the window's reviews; one theme row per cluster
    label      the small model names each theme; themes ranked by count
    quotes     the model picks 3 review ids; code copies and verifies the text
    fees       fee lexicon detector (code only)
    explainer  fee explainer from the corpus, if a mutual fund fee issue was found
    pulse      the weekly pulse, at most 250 words with 3 action ideas
    actions    pending doc append, email draft and (with an explainer) promote_canonical

Validation runs inside the explainer and pulse stages: one regeneration with the named
failure, then the run is marked failed with the reason.
"""

import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.corpus.embed import Embedder
from app.db.models import FeeExplainer, PendingAction, Pulse, Review, Theme, WeeklyRun
from app.gate.actions import new_pending_action
from app.observability import get_logger
from app.policy import PolicyContext, ThemeArtifact, run_policy
from app.policy.text import count_words
from app.weekly.cluster import cluster_embeddings
from app.weekly.fees import BY_KEY, FeeDetection, FeeReview, detect_fee_confusion
from app.weekly.generate import (
    Completer,
    PolicyFailure,
    PulseInput,
    PulseTheme,
    Quote,
    Retriever,
    check_explainer,
    check_pulse,
    explainer_context,
    explainer_passages,
    generate_validated,
    label_theme,
    pick_quotes,
    quote_candidates,
    verify_quotes,
    write_explainer,
    write_pulse,
)
from app.weekly.reviews import ingest_reviews, load_csv

log = get_logger(__name__)

STAGES = ("ingest", "cluster", "label", "quotes", "fees", "explainer", "pulse", "actions")
TOP_THEMES = 3
ACTIVE_STATUSES = ("pending", "in_progress")
_WORD = re.compile(r"\w+")
_LOCK_KEY = 0x5EE41  # advisory lock: one weekly run created at a time


@dataclass
class WeeklyDeps:
    sessions: async_sessionmaker[AsyncSession]
    llm: Completer
    embedder: Embedder
    retriever_for: Callable[[AsyncSession], Retriever]
    settings: Settings
    csv_path: Path | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))

    @property
    def reviews_csv(self) -> Path:
        return self.csv_path or self.settings.reviews_csv_path


class QuoteError(RuntimeError):
    pass


# --- Creating and finding runs -----------------------------------------------------------


def window_for(end: date, weeks: int) -> tuple[date, date]:
    return end - timedelta(weeks=weeks) + timedelta(days=1), end


async def active_run(session: AsyncSession) -> WeeklyRun | None:
    return await session.scalar(
        select(WeeklyRun)
        .where(WeeklyRun.status.in_(ACTIVE_STATUSES))
        .order_by(WeeklyRun.started_at.desc())
        .limit(1)
    )


async def create_run(
    session: AsyncSession, *, window_end: date, weeks: int, triggered_by: str
) -> tuple[WeeklyRun, bool]:
    """A new pending run, or the run already in progress. Returns (run, created)."""
    await session.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _LOCK_KEY})
    existing = await active_run(session)
    if existing is not None:
        return existing, False
    start, end = window_for(window_end, weeks)
    run = WeeklyRun(window_start=start, window_end=end, triggered_by=triggered_by,
                    status="pending", stage_log={})  # fmt: skip
    session.add(run)
    await session.flush()
    return run, True


# --- Running ------------------------------------------------------------------------------


async def execute_run(run_id: uuid.UUID, deps: WeeklyDeps) -> str:
    """Run every stage not yet done. Returns the run's final status."""
    async with deps.sessions() as s:
        run = await s.get(WeeklyRun, run_id)
        if run is None:
            raise ValueError(f"weekly run {run_id} not found")
        if run.status not in ACTIVE_STATUSES:
            return run.status
        run.status = "in_progress"
        await s.commit()

    for stage in STAGES:
        async with deps.sessions() as s:
            run = await s.get(WeeklyRun, run_id)
            assert run is not None
            if (run.stage_log.get(stage) or {}).get("status") == "done":
                continue
            started = deps.now()
            try:
                summary = await _STAGE_FUNCS[stage](s, run, deps)
            except Exception as exc:  # noqa: BLE001 - every failure is recorded on the run
                await s.rollback()
                await _mark_failed(deps, run_id, stage, exc)
                return "failed"
            run.stage_log = {
                **run.stage_log,
                stage: {"status": "done", "started_at": started.isoformat(),
                        "finished_at": deps.now().isoformat(), **summary},
            }  # fmt: skip
            await s.commit()
            log.info("weekly_stage_done", run_id=str(run_id), stage=stage)
    return "awaiting_approval"


async def _mark_failed(deps: WeeklyDeps, run_id: uuid.UUID, stage: str, exc: Exception) -> None:
    if isinstance(exc, PolicyFailure):
        reason = str(exc)
        detail: dict[str, Any] = {"rules": [{"rule": r.rule, "detail": r.detail}
                                            for r in exc.results]}  # fmt: skip
    else:
        reason = f"{type(exc).__name__}: {exc}"[:500]
        detail = {}
    async with deps.sessions() as s:
        run = await s.get(WeeklyRun, run_id)
        assert run is not None
        run.status = "failed"
        run.error = f"{stage}: {reason}"
        run.finished_at = deps.now()
        run.stage_log = {**run.stage_log, stage: {"status": "failed", "error": reason, **detail}}
        await s.commit()
    log.warning("weekly_run_failed", run_id=str(run_id), stage=stage, error=type(exc).__name__)


# --- Stages -------------------------------------------------------------------------------

StageFunc = Callable[[AsyncSession, WeeklyRun, WeeklyDeps], Awaitable[dict[str, Any]]]


async def _run_reviews(s: AsyncSession, run: WeeklyRun) -> list[Review]:
    """The reviews this run's ingest stage stored (in window, deduplicated)."""
    ids = run.stage_log["ingest"]["review_ids"]
    rows = await s.scalars(
        select(Review).where(Review.review_id.in_(ids)).order_by(Review.date.desc(),
                                                                 Review.review_id)
    )  # fmt: skip
    return list(rows.all())


async def _themes(s: AsyncSession, run: WeeklyRun) -> list[Theme]:
    rows = await s.scalars(select(Theme).where(Theme.run_id == run.id))
    return sorted(rows.all(), key=lambda t: (t.rank or 99, -t.count, str(t.id)))


async def stage_ingest(s: AsyncSession, run: WeeklyRun, deps: WeeklyDeps) -> dict[str, Any]:
    rows = load_csv(deps.reviews_csv)
    stats = await ingest_reviews(
        s, rows, deps.embedder,
        window_start=run.window_start, window_end=run.window_end,
        dedupe_similarity=deps.settings.review_dedupe_similarity,
    )  # fmt: skip
    return {"csv": deps.reviews_csv.name, **stats.as_dict()}


async def stage_cluster(s: AsyncSession, run: WeeklyRun, deps: WeeklyDeps) -> dict[str, Any]:
    reviews = await _run_reviews(s, run)
    min_words = deps.settings.theme_min_words
    themed = [
        r
        for r in reviews
        if r.embedding is not None and len(_WORD.findall(r.text_redacted)) >= min_words
    ]
    result = cluster_embeddings(
        [list(r.embedding) for r in themed],  # type: ignore[arg-type]
        min_k=deps.settings.weekly_min_themes,
        max_k=deps.settings.weekly_max_themes,
        seed=deps.settings.weekly_cluster_seed,
    )
    await s.execute(delete(Theme).where(Theme.run_id == run.id))
    for c in result.clusters:
        s.add(Theme(run_id=run.id, label="", count=len(c.members), centroid=c.centroid,
                    review_ids=[themed[i].review_id for i in c.members]))  # fmt: skip
    return {
        "reviews": len(reviews),
        "clustered": len(themed),
        "too_short": len(reviews) - len(themed),
        "min_words": min_words,
        "k": result.k,
        "silhouette": result.silhouette,
        "silhouette_by_k": {str(k): v for k, v in result.scores.items()},
    }


async def stage_label(s: AsyncSession, run: WeeklyRun, deps: WeeklyDeps) -> dict[str, Any]:
    themes = sorted(await _themes(s, run), key=lambda t: (-t.count, str(t.id)))
    texts = await _review_texts(s, [rid for t in themes for rid in t.review_ids[:8]])
    labelled = []
    for rank, theme in enumerate(themes, start=1):
        examples = [texts[rid] for rid in theme.review_ids[:8] if rid in texts]

        def check(label: Any, theme: Theme = theme) -> list[Any]:
            return run_policy(
                ThemeArtifact(
                    label.label,
                    theme.count,
                    tuple(theme.review_ids),
                    (),
                    run.window_start,
                    run.window_end,
                ),  # fmt: skip
                PolicyContext.from_settings(deps.settings),
            )

        async def make(feedback: str | None, examples: list[str] = examples) -> Any:
            return await label_theme(deps.llm, examples, feedback)

        result = await generate_validated("theme label", make, check)
        label = result.value
        theme.label, theme.description, theme.rank = label.label, label.description, rank
        labelled.append({"rank": rank, "label": label.label, "count": theme.count,
                         **result.log()})  # fmt: skip
    return {"themes": labelled}


async def _review_texts(s: AsyncSession, ids: list[str]) -> dict[str, str]:
    if not ids:
        return {}
    rows = await s.execute(
        select(Review.review_id, Review.text_redacted).where(Review.review_id.in_(ids))
    )
    return {rid: t for rid, t in rows.all()}


async def stage_quotes(s: AsyncSession, run: WeeklyRun, deps: WeeklyDeps) -> dict[str, Any]:
    top = (await _themes(s, run))[:TOP_THEMES]
    texts = await _review_texts(s, [rid for t in top for rid in t.review_ids])
    candidates = quote_candidates(
        [[(rid, texts[rid]) for rid in t.review_ids if rid in texts] for t in top]
    )
    quotes, how = await pick_quotes(deps.llm, [t.label for t in top], candidates)
    problems = verify_quotes(quotes, texts)
    if problems:
        raise QuoteError("; ".join(problems))
    return {
        "picked_by": how,
        "quotes": [{"review_id": q.review_id, "theme_id": str(top[q.theme_index].id),
                    "theme": top[q.theme_index].label, "text": q.text} for q in quotes],
    }  # fmt: skip


async def stage_fees(s: AsyncSession, run: WeeklyRun, deps: WeeklyDeps) -> dict[str, Any]:
    reviews = await _run_reviews(s, run)
    detection = detect_fee_confusion(
        [FeeReview(r.review_id, r.rating, r.text_redacted) for r in reviews],
        min_reviews=deps.settings.fee_min_reviews,
    )
    return detection.as_dict()


async def stage_explainer(s: AsyncSession, run: WeeklyRun, deps: WeeklyDeps) -> dict[str, Any]:
    await s.execute(delete(FeeExplainer).where(FeeExplainer.run_id == run.id))
    fees = run.stage_log["fees"]
    if fees["fee_type"] is None:
        return {"skipped": "no fee confusion detected"}
    fee = BY_KEY[fees["fee_type"]]
    passages = await explainer_passages(
        deps.retriever_for(s), fee, min_similarity=deps.settings.retrieval_min_similarity
    )
    if len({p.url for p in passages}) < 2:
        return {"skipped": "fewer than two corpus sources cover this fee", "fee_type": fee.key}
    ctx = explainer_context(passages, tuple(deps.settings.source_domains), deps.now().date())

    async def make(feedback: str | None) -> Any:
        return await write_explainer(deps.llm, fee, passages, feedback)

    result = await generate_validated("fee explainer", make, lambda d: check_explainer(d, ctx))
    draft = result.value
    assert draft.last_checked is not None  # the policy engine requires it
    s.add(FeeExplainer(run_id=run.id, fee_type=fee.key, bullets=list(draft.bullets),
                       source_urls=list(draft.source_urls), last_checked=draft.last_checked,
                       evidence_review_ids=fees["evidence_review_ids"]))  # fmt: skip
    return {
        "fee_type": fee.key,
        **result.log(),
        "bullets": len(draft.bullets),
        "dropped_bullets": draft.dropped_bullets,
        "sources": list(draft.source_keys),
    }


def _detection(stage: dict[str, Any]) -> FeeDetection:
    from app.weekly.fees import FeeCount

    counts = [FeeCount(c["fee_type"], c["label"], c["mutual_fund"], list(c["review_ids"]),
                       c["score"]) for c in stage["counts"]]  # fmt: skip
    return FeeDetection(stage["fee_type"], stage["label"], list(stage["evidence_review_ids"]),
                        counts, stage["min_reviews"])  # fmt: skip


async def stage_pulse(s: AsyncSession, run: WeeklyRun, deps: WeeklyDeps) -> dict[str, Any]:
    top = (await _themes(s, run))[:TOP_THEMES]
    explainer = await s.scalar(select(FeeExplainer).where(FeeExplainer.run_id == run.id))
    quotes = [Quote(q["review_id"], 0, q["text"]) for q in run.stage_log["quotes"]["quotes"]]
    labels = [q["theme"] for q in run.stage_log["quotes"]["quotes"]]
    cluster = run.stage_log["cluster"]
    inp = PulseInput(
        window_start=run.window_start,
        window_end=run.window_end,
        reviews_analysed=cluster["reviews"],
        reviews_themed=cluster["clustered"],
        themes=[PulseTheme(t.label, t.description or "", t.count) for t in top],
        quotes=[(q.text, label) for q, label in zip(quotes, labels, strict=True)],
        fees=_detection(run.stage_log["fees"]),
        explainer_attached=explainer is not None,
    )
    ctx = PolicyContext.from_settings(deps.settings, today=deps.now().date())

    async def make(feedback: str | None) -> str:
        return await write_pulse(deps.llm, inp, feedback)

    result = await generate_validated("weekly pulse", make, lambda t: check_pulse(t, ctx))
    pulse_text = result.value
    await s.execute(delete(Pulse).where(Pulse.run_id == run.id))
    words = count_words(pulse_text)
    s.add(Pulse(run_id=run.id, text_redacted=pulse_text, word_count=words,
                quote_review_ids=[q.review_id for q in quotes]))  # fmt: skip
    return {**result.log(), "word_count": words}


async def stage_actions(s: AsyncSession, run: WeeklyRun, deps: WeeklyDeps) -> dict[str, Any]:
    await s.execute(
        delete(PendingAction).where(
            PendingAction.origin == "weekly_run",
            PendingAction.origin_id == run.id,
            PendingAction.status == "pending",
        )
    )
    pulse = await s.scalar(select(Pulse).where(Pulse.run_id == run.id))
    explainer = await s.scalar(select(FeeExplainer).where(FeeExplainer.run_id == run.id))
    assert pulse is not None
    top = (await _themes(s, run))[:TOP_THEMES]
    payloads = build_payloads(run, pulse, explainer, top, deps.now().date())
    kinds = []
    for kind, target, payload in (
        ("doc_append", deps.settings.google_doc_id or "google_doc_unset", payloads["doc_append"]),
        ("email_draft", "gmail_drafts", payloads["email_draft"]),
        ("promote_canonical", "internal", payloads.get("promote_canonical")),
    ):
        if payload is None:
            continue
        new_pending_action(s, kind=kind, origin="weekly_run", origin_id=run.id,  # type: ignore[arg-type]
                           target=target, payload=payload)  # fmt: skip
        kinds.append(kind)
    run.status = "awaiting_approval"
    run.finished_at = deps.now()
    return {"actions": kinds}


def build_payloads(
    run: WeeklyRun, pulse: Pulse, explainer: FeeExplainer | None, top: list[Theme], today: date
) -> dict[str, dict[str, Any] | None]:
    """Exactly what each action would write (architecture section 10.3)."""
    fees = run.stage_log["fees"]
    fee_issue = fees["label"] if fees["fee_type"] else "No fee confusion detected this week"
    bullets = list(explainer.bullets) if explainer else []
    links = list(explainer.source_urls) if explainer else []
    last_checked = explainer.last_checked.isoformat() if explainer else None
    doc = {
        "date": today.isoformat(),
        "window": {"start": run.window_start.isoformat(), "end": run.window_end.isoformat()},
        "top_themes": [
            {"label": t.label, "description": t.description, "count": t.count} for t in top
        ],  # fmt: skip
        "weekly_pulse": pulse.text_redacted,
        "identified_fee_issue": fee_issue,
        "explanation_bullets": bullets,
        "source_links": links,
        "last_checked": last_checked,
    }
    body = pulse.text_redacted
    if explainer is not None:
        snippet = "\n".join(f"- {b}" for b in bullets)
        sources = "\n".join(links)
        body += (f"\n\nCustomer clarification ({fee_issue}):\n{snippet}\n\nSources:\n{sources}"
                 f"\nLast checked: {last_checked}")  # fmt: skip
    email = {
        "subject": f"Weekly Product Pulse + Customer Clarification — {today.isoformat()}",
        "body": body,
        "to": None,  # no recipient: the person sending adds it in Gmail
    }
    promote = None
    if explainer is not None:
        promote = {
            "fee_type": explainer.fee_type,
            "explainer_id": str(explainer.id),
            "bullets": bullets,
            "source_urls": links,
            "last_checked": last_checked,
        }
    return {"doc_append": doc, "email_draft": email, "promote_canonical": promote}


_STAGE_FUNCS: dict[str, StageFunc] = {
    "ingest": stage_ingest,
    "cluster": stage_cluster,
    "label": stage_label,
    "quotes": stage_quotes,
    "fees": stage_fees,
    "explainer": stage_explainer,
    "pulse": stage_pulse,
    "actions": stage_actions,
}
