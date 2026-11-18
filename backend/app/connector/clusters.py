"""Rolling question clusters (architecture section 9.1).

Each new queue item joins the nearest cluster seen in the last `active_days` whose centroid is
at least `threshold` similar, or starts a new cluster. The centroid is the running mean of its
members' embeddings.

Surfacing (architecture section 9.1): a cluster with 5 or more items in the last 7 days is
marked `surfaced`, and the next weekly run must report it. Surfacing is checked each time an
item joins, and it is never undone: a cluster that was surfaced stays reportable.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

import numpy as np
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import QuestionCluster, QueueItem, Theme, WeeklyRun

SURFACE_MIN_ITEMS = 5
SURFACE_DAYS = 7


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{v:.6f}" for v in vector) + "]"


def _unit(v: np.ndarray) -> list[float]:
    n = float(np.linalg.norm(v))
    return [float(x) for x in (v / n if n else v)]


async def assign_cluster(
    s: AsyncSession,
    item: QueueItem,
    embedding: list[float],
    *,
    threshold: float,
    active_days: int,
    now: datetime,
) -> tuple[uuid.UUID, bool]:
    """Put `item` in a cluster. Returns (cluster id, created)."""
    row = (
        await s.execute(
            text(
                "SELECT id, 1 - (centroid <=> CAST(:v AS vector)) AS sim FROM question_cluster "
                "WHERE centroid IS NOT NULL AND last_seen >= :since "
                "ORDER BY centroid <=> CAST(:v AS vector) LIMIT 1"
            ),
            {"v": vector_literal(embedding), "since": now - timedelta(days=active_days)},
        )
    ).first()
    if row is not None and float(row.sim) >= threshold:
        cluster = await s.get(QuestionCluster, row.id)
        assert cluster is not None
        members = await s.scalar(
            select(func.count()).select_from(QueueItem).where(QueueItem.cluster_id == cluster.id)
        )
        n = int(members or 0)
        old = np.asarray(cluster.centroid, dtype=np.float64)
        cluster.centroid = _unit(old * n + np.asarray(embedding, dtype=np.float64))
        cluster.last_seen = now
        item.cluster_id = cluster.id
        await s.flush()
        await refresh_surfaced(s, cluster, now=now)
        return cluster.id, False
    cluster = QuestionCluster(centroid=_unit(np.asarray(embedding, dtype=np.float64)),
                              first_seen=now, last_seen=now)  # fmt: skip
    s.add(cluster)
    await s.flush()
    item.cluster_id = cluster.id
    return cluster.id, True


async def recent_members(
    s: AsyncSession, cluster_id: uuid.UUID, *, now: datetime, days: int = SURFACE_DAYS
) -> int:
    count = await s.scalar(
        select(func.count())
        .select_from(QueueItem)
        .where(
            QueueItem.cluster_id == cluster_id, QueueItem.created_at >= now - timedelta(days=days)
        )
    )
    return int(count or 0)


async def refresh_surfaced(s: AsyncSession, cluster: QuestionCluster, *, now: datetime) -> bool:
    """Mark the cluster surfaced once it has SURFACE_MIN_ITEMS items in SURFACE_DAYS days.
    Returns True when this call surfaced it."""
    if cluster.surfaced:
        return False
    if await recent_members(s, cluster.id, now=now) < SURFACE_MIN_ITEMS:
        return False
    cluster.surfaced = True
    cluster.surfaced_at = now
    return True


@dataclass(frozen=True)
class SurfacedCluster:
    cluster_id: uuid.UUID
    items: list[QueueItem]  # oldest first

    @property
    def window(self) -> tuple[date, date]:
        return self.items[0].created_at.date(), self.items[-1].created_at.date()


async def surfaced_for_run(
    s: AsyncSession,
    *,
    run_id: uuid.UUID,
    window_start: date,
    window_end: date,
) -> list[SurfacedCluster]:
    """The surfaced clusters a weekly run must report, largest first.

    A cluster is reported by the first run that ends after it surfaced. A later run reports it
    again only while it is still active: SURFACE_MIN_ITEMS new items in the run's last
    SURFACE_DAYS days. Each report lists the cluster's items inside the run's window, with
    their count and first and last dates.
    """
    start = datetime.combine(window_start, time.min, UTC)
    until = datetime.combine(window_end + timedelta(days=1), time.min, UTC)
    recent_since = until - timedelta(days=SURFACE_DAYS)
    clusters = (
        await s.scalars(
            select(QuestionCluster).where(
                QuestionCluster.surfaced, QuestionCluster.surfaced_at < until
            )
        )
    ).all()
    if not clusters:
        return []
    reported = set(
        (
            await s.scalars(
                select(Theme.cluster_id)
                .join(WeeklyRun, WeeklyRun.id == Theme.run_id)
                .where(
                    Theme.cluster_id.in_([c.id for c in clusters]),
                    Theme.run_id != run_id,
                    # A run that failed never reached anyone, so it doesn't count as reported.
                    WeeklyRun.status.in_(("awaiting_approval", "completed")),
                )
            )
        ).all()
    )
    out = []
    for cluster in clusters:
        items = list(
            (
                await s.scalars(
                    select(QueueItem)
                    .where(
                        QueueItem.cluster_id == cluster.id,
                        QueueItem.created_at >= start,
                        QueueItem.created_at < until,
                    )
                    .order_by(QueueItem.created_at, QueueItem.id)
                )
            ).all()
        )
        recent = sum(1 for i in items if i.created_at >= recent_since)
        if not items or (cluster.id in reported and recent < SURFACE_MIN_ITEMS):
            continue
        out.append(SurfacedCluster(cluster.id, items))
    return sorted(out, key=lambda c: (-len(c.items), str(c.cluster_id)))
