"""Code-only parts of the weekly loop: CSV loading, dedupe, clustering, fee detection."""

import re
from datetime import date

import numpy as np
import pytest

from app.corpus.embed import HashEmbedder
from app.weekly.cluster import cluster_embeddings, kmeans, pca, silhouette
from app.weekly.fees import FeeReview, detect_fee_confusion, fee_types_in
from app.weekly.reviews import ReviewCsvError, dedupe, load_csv
from tests.weekly.conftest import FIXTURE_CSV, NO_FEES_CSV

_WORD = re.compile(r"\w+")


def _themed_fixture() -> tuple[list[str], list[list[float]]]:
    rows = [r for r in load_csv(FIXTURE_CSV) if r.review_id.split(":")[1] in
            ("payments", "support", "speed")]  # fmt: skip
    return [r.review_id for r in rows], HashEmbedder().embed_documents([r.text for r in rows])


def test_csv_rows_pass_through_the_ingress_guard() -> None:
    rows = {r.review_id: r for r in load_csv(FIXTURE_CSV)}
    pii = rows["fx:pii:0"]
    assert "ABCDE1234F" not in pii.text and "[PAN_REDACTED]" in pii.text
    assert "pan" in pii.pii_flags
    assert rows["fx:payments:0"].pii_flags == ()


def test_csv_missing_column_is_an_error(tmp_path: object) -> None:
    from pathlib import Path

    bad = Path(str(tmp_path)) / "bad.csv"
    bad.write_text("review_id,text\nx,hello\n")
    with pytest.raises(ReviewCsvError, match="missing columns"):
        load_csv(bad)


def test_dedupe_drops_exact_and_near_duplicates_keeping_the_newest() -> None:
    rows = load_csv(FIXTURE_CSV)
    vectors = HashEmbedder().embed_documents([r.text for r in rows])
    keep, exact, near = dedupe(rows, vectors, threshold=0.95)
    kept_ids = {rows[i].review_id for i in keep}
    # fx:dup:0 repeats fx:payments:0 word for word; the newer one is kept.
    assert exact == 1
    dates = {r.review_id: r.date for r in rows}
    newer, older = sorted(["fx:dup:0", "fx:payments:0"], key=dates.__getitem__, reverse=True)
    assert newer in kept_ids and older not in kept_ids
    assert near == 0


def test_fixed_fixture_gives_reproducible_clusters_matching_its_topics() -> None:
    ids, vectors = _themed_fixture()
    first = cluster_embeddings(vectors, min_k=3, max_k=5, seed=42)
    second = cluster_embeddings(vectors, min_k=3, max_k=5, seed=42)
    assert first == second
    assert first.k == 3
    groups = [{ids[i].split(":")[1] for i in c.members} for c in first.clusters]
    assert sorted(len(g) for g in groups) == [1, 1, 1]  # each cluster is exactly one topic
    assert sum(len(c.members) for c in first.clusters) == len(ids)
    for c in first.clusters:
        assert abs(float(np.linalg.norm(c.centroid)) - 1.0) < 1e-6


def test_silhouette_prefers_the_true_number_of_clusters() -> None:
    rng = np.random.default_rng(0)
    centres = np.array([[0.0, 0.0], [10.0, 0.0], [0.0, 10.0]])
    x = np.vstack([c + rng.normal(scale=0.5, size=(20, 2)) for c in centres])
    good, _ = kmeans(x, 3, np.random.default_rng(1))
    bad, _ = kmeans(x, 2, np.random.default_rng(1))
    assert silhouette(x, good) > silhouette(x, bad) > 0
    assert pca(x, 2).shape == (60, 2)


def test_too_few_reviews_give_one_theme() -> None:
    result = cluster_embeddings([[1.0, 0.0], [0.0, 1.0]], max_k=5, seed=1)
    assert result.k == 1 and result.clusters[0].members == [0, 1]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("exit load was deducted on redemption", ["exit_load"]),
        ("got less amount after I sold my mutual fund units", ["exit_load"]),
        ("forces Regular Plan instead of Direct Growth, more commission", ["expense_ratio"]),
        ("stamp duty was cut from my SIP", ["stamp_duty"]),
        ("brokerage charges are too high", ["brokerage"]),
        ("hidden charges on US stocks withdrawal", ["other_charges"]),
        ("withdraw takes days and charges are high", ["other_charges"]),  # no mutual fund context
        ("the app is slow", []),
    ],
)
def test_fee_lexicon(text: str, expected: list[str]) -> None:
    assert fee_types_in(text) == expected


def test_detector_picks_the_top_mutual_fund_fee_with_evidence() -> None:
    reviews = [FeeReview(r.review_id, r.rating, r.text) for r in load_csv(FIXTURE_CSV)]
    result = detect_fee_confusion(reviews, min_reviews=3)
    assert result.fee_type == "exit_load"
    assert sorted(result.evidence_review_ids) == [f"fx:fees:{i}" for i in range(4)]
    assert result.as_dict()["counts"][0]["fee_type"] == "exit_load"


def test_no_fee_fixture_records_no_fee_confusion() -> None:
    reviews = [FeeReview(r.review_id, r.rating, r.text) for r in load_csv(NO_FEES_CSV)]
    result = detect_fee_confusion(reviews, min_reviews=3)
    assert result.fee_type is None and result.evidence_review_ids == []
    assert result.message == "No fee confusion detected this week."


def test_platform_charges_never_become_the_fee_issue() -> None:
    reviews = [FeeReview(str(i), 1, "brokerage charges are too high") for i in range(10)]
    reviews += [FeeReview("mf1", 5, "exit load on redemption")]
    result = detect_fee_confusion(reviews, min_reviews=3)
    assert result.fee_type is None  # one exit load mention is below the minimum
    assert result.counts[0].key == "brokerage" and not result.counts[0].mutual_fund


def test_low_ratings_weigh_more() -> None:
    reviews = [FeeReview("a", 1, "exit load"), FeeReview("b", 1, "exit load"),
               FeeReview("c", 1, "exit load"), FeeReview("d", 5, "stamp duty"),
               FeeReview("e", 5, "stamp duty"), FeeReview("f", 5, "stamp duty"),
               FeeReview("g", 5, "stamp duty")]  # fmt: skip
    assert detect_fee_confusion(reviews, min_reviews=3).fee_type == "exit_load"  # 4.5 vs 4.0


def test_window_dates_in_fixture() -> None:
    rows = load_csv(FIXTURE_CSV)
    assert min(r.date for r in rows) == date(2026, 1, 5)  # the out-of-window row
