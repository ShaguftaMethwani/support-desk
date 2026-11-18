"""Cluster review embeddings into themes: PCA, then KMeans for k = min_k..max_k, k chosen by
silhouette score (architecture section 8.1).

k starts at 3, not 2: the pulse reports a top 3, and on the real review sample k = 2 always
won with a weak score (about 0.12) that only split praise from complaints.

Written with numpy only. scikit-learn would add a large dependency for three small
algorithms, and the API has to fit in 512 MB on the free hosting tier. Every random choice
comes from one seeded generator, so the same reviews always give the same clusters.
"""

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

PCA_COMPONENTS = 20
KMEANS_RESTARTS = 10
KMEANS_MAX_ITER = 300

Matrix = NDArray[np.float64]


@dataclass(frozen=True)
class Cluster:
    members: list[int]  # row indices into the input, closest to the centre first
    centroid: list[float]  # mean of the members' original embeddings, unit length


@dataclass(frozen=True)
class Clustering:
    k: int
    silhouette: float | None  # None when there were too few items to compare k values
    scores: dict[int, float]  # silhouette per k tried
    clusters: list[Cluster]


def pca(x: Matrix, components: int = PCA_COMPONENTS) -> Matrix:
    centred = x - x.mean(axis=0)
    n = min(components, centred.shape[0] - 1, centred.shape[1])
    if n < 1:
        return centred
    # Rows of vt are the principal axes. The sign of each axis is fixed by its largest
    # entry, so the projection doesn't flip between machines or library versions.
    _, _, vt = np.linalg.svd(centred, full_matrices=False)
    axes = vt[:n]
    signs = np.sign(axes[np.arange(n), np.abs(axes).argmax(axis=1)])
    signs[signs == 0] = 1.0
    return np.asarray(centred @ (axes * signs[:, None]).T, dtype=np.float64)


def _sq_dist(x: Matrix, centres: Matrix) -> Matrix:
    d = (x * x).sum(axis=1)[:, None] - 2 * x @ centres.T + (centres * centres).sum(axis=1)[None]
    return np.maximum(d, 0.0)


def _kmeans_pp(x: Matrix, k: int, rng: np.random.Generator) -> Matrix:
    centres = [x[rng.integers(len(x))]]
    for _ in range(1, k):
        d = _sq_dist(x, np.array(centres)).min(axis=1)
        total = d.sum()
        idx = rng.integers(len(x)) if total == 0 else rng.choice(len(x), p=d / total)
        centres.append(x[idx])
    return np.array(centres)


def kmeans(x: Matrix, k: int, rng: np.random.Generator) -> tuple[NDArray[np.int64], Matrix]:
    """Best of several k-means++ starts, by within-cluster sum of squares."""
    best: tuple[float, NDArray[np.int64], Matrix] | None = None
    for _ in range(KMEANS_RESTARTS):
        centres = _kmeans_pp(x, k, rng)
        labels = np.zeros(len(x), dtype=np.int64)
        for _ in range(KMEANS_MAX_ITER):
            labels = _sq_dist(x, centres).argmin(axis=1)
            moved = centres.copy()
            for j in range(k):
                members = x[labels == j]
                # An empty cluster takes the point furthest from its centre.
                moved[j] = (
                    members.mean(axis=0)
                    if len(members)
                    else x[_sq_dist(x, centres).min(axis=1).argmax()]
                )
            if np.allclose(moved, centres):
                break
            centres = moved
        inertia = float(_sq_dist(x, centres)[np.arange(len(x)), labels].sum())
        if best is None or inertia < best[0] - 1e-9:
            best = (inertia, labels, centres)
    assert best is not None
    return best[1], best[2]


def silhouette(x: Matrix, labels: NDArray[np.int64]) -> float:
    """Mean silhouette coefficient, Euclidean distance. Single-member clusters score 0."""
    dist = np.sqrt(_sq_dist(x, x))
    ks = np.unique(labels)
    scores = np.zeros(len(x))
    for i in range(len(x)):
        own = labels == labels[i]
        n_own = own.sum() - 1
        if n_own == 0:
            continue
        a = dist[i, own].sum() / n_own
        b = min(dist[i, labels == other].mean() for other in ks if other != labels[i])
        scores[i] = (b - a) / max(a, b) if max(a, b) > 0 else 0.0
    return float(scores.mean())


def cluster_embeddings(
    embeddings: list[list[float]], *, min_k: int = 2, max_k: int, seed: int
) -> Clustering:
    x = np.asarray(embeddings, dtype=np.float64)
    if len(x) < 4:  # too few to compare clusterings: one theme holding everything
        centroid = x.mean(axis=0) if len(x) else np.zeros(0)
        return Clustering(1, None, {}, [Cluster(list(range(len(x))), _unit(centroid))])
    reduced = pca(x)
    rng = np.random.default_rng(seed)
    scores: dict[int, float] = {}
    fits: dict[int, tuple[NDArray[np.int64], Matrix]] = {}
    for k in range(max(2, min_k), min(max_k, len(x) - 1) + 1):
        labels, centres = kmeans(reduced, k, rng)
        if len(np.unique(labels)) < k:
            continue
        scores[k] = round(silhouette(reduced, labels), 6)
        fits[k] = (labels, centres)
    if not scores:
        return Clustering(1, None, {}, [Cluster(list(range(len(x))), _unit(x.mean(axis=0)))])
    best_k = max(scores, key=lambda k: (scores[k], -k))  # ties go to fewer themes
    labels, centres = fits[best_k]
    clusters = []
    for j in range(best_k):
        idx = np.flatnonzero(labels == j)
        d = _sq_dist(reduced[idx], centres[j : j + 1])[:, 0]
        order = idx[np.lexsort((idx, d))]  # closest first, index breaks ties
        clusters.append(Cluster([int(i) for i in order], _unit(x[idx].mean(axis=0))))
    return Clustering(best_k, scores[best_k], scores, clusters)


def _unit(v: NDArray[np.float64]) -> list[float]:
    norm = float(np.linalg.norm(v))
    return [float(a) for a in (v / norm if norm else v)]
