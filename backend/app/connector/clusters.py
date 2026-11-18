"""Rolling question clusters (architecture section 9.1).

Each new queue item joins the nearest cluster seen in the last `active_days` whose centroid is
at least `threshold` similar, or starts a new cluster. The centroid is the running mean of its
members' embeddings. Phase 6 adds surfacing (5 or more items in 7 days) on top of this.
"""

import uuid
from datetime import datetime, timedelta

import numpy as np
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import QuestionCluster, QueueItem


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
        return cluster.id, False
    cluster = QuestionCluster(centroid=_unit(np.asarray(embedding, dtype=np.float64)),
                              first_seen=now, last_seen=now)  # fmt: skip
    s.add(cluster)
    await s.flush()
    item.cluster_id = cluster.id
    return cluster.id, True
