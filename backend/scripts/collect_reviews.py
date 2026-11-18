"""Collect recent INDMoney app reviews into data/reviews_sample.csv (PII removed).

Run from backend/ (the Play Store library is not a project dependency, it is only needed here):
    uv run --with google-play-scraper python -m scripts.collect_reviews --weeks 12

- Play Store: the `google-play-scraper` package, newest first, until reviews are older than the
  window. App Store: Apple's public customer-reviews RSS feed (up to 500 most recent).
- Only review id, store, date, rating and text are kept. Author names and profile data are
  never read.
- Every review text goes through the ingress guard before anything is written, so raw text
  never reaches the disk. The weekly loop runs the guard again on ingest.
"""

import argparse
import csv
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from app.config import REPO_ROOT
from app.guard import guard

PLAY_PACKAGE = "in.indwealth"
APP_STORE_ID = "1450178837"
APP_STORE_RSS = "https://itunes.apple.com/rss/customerreviews/page={page}/id={app_id}/sortby=mostrecent/json?cc=in"
OUT = REPO_ROOT / "data" / "reviews_sample.csv"
FIELDS = ["review_id", "source", "date", "rating", "text"]


@dataclass(frozen=True)
class RawReview:
    review_id: str
    source: str  # play_store | app_store
    date: date
    rating: int | None
    text: str


def play_store(since: date) -> Iterator[RawReview]:
    from google_play_scraper import Sort, reviews  # type: ignore[import-not-found]

    token = None
    while True:
        batch, token = reviews(
            PLAY_PACKAGE, lang="en", country="in", sort=Sort.NEWEST, count=200,
            continuation_token=token,
        )  # fmt: skip
        if not batch:
            return
        for r in batch:
            when = r["at"].date()
            if when < since:
                return
            yield RawReview(f"play:{r['reviewId']}", "play_store", when, r.get("score"),
                            r.get("content") or "")  # fmt: skip
        if token is None or getattr(token, "token", None) is None:
            return
        time.sleep(1.0)


def app_store(since: date) -> Iterator[RawReview]:
    with httpx.Client(timeout=30, headers={"User-Agent": "SupportDeskReviewSample/0.1"}) as client:
        for page in range(1, 11):  # the feed serves at most 10 pages of 50
            response = client.get(APP_STORE_RSS.format(page=page, app_id=APP_STORE_ID))
            if response.status_code != 200:
                return
            entries = response.json().get("feed", {}).get("entry", [])
            if isinstance(entries, dict):
                entries = [entries]
            reviews = [e for e in entries if "im:rating" in e]  # the first entry can be the app
            if not reviews:
                return
            for e in reviews:
                when = datetime.fromisoformat(e["updated"]["label"]).date()
                if when < since:
                    continue
                title = (e.get("title", {}).get("label") or "").strip()
                body = (e.get("content", {}).get("label") or "").strip()
                text = f"{title}. {body}" if title and body else (title or body)
                yield RawReview(f"appstore:{e['id']['label']}", "app_store", when,
                                int(e["im:rating"]["label"]), text)  # fmt: skip
            time.sleep(1.0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    parser.add_argument("--weeks", type=int, default=12, help="how far back to collect")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    since = datetime.now(UTC).date() - timedelta(weeks=args.weeks)
    rows: list[dict[str, object]] = []
    redacted = 0
    seen: set[str] = set()
    for source in (play_store, app_store):
        count = 0
        for r in source(since):
            text = " ".join(r.text.split())
            if not text or r.review_id in seen:
                continue
            seen.add(r.review_id)
            clean = guard(text)
            redacted += clean.redaction_count
            rows.append({"review_id": r.review_id, "source": r.source, "date": r.date.isoformat(),
                         "rating": r.rating, "text": clean.text})  # fmt: skip
            count += 1
        print(f"{source.__name__:12} {count} reviews since {since}")
    rows.sort(key=lambda row: (str(row["date"]), str(row["review_id"])), reverse=True)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows)} reviews to {args.out} ({redacted} PII values redacted)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
