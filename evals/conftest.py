"""Shared fixtures for the eval suites. run_all.py is the usual entry point; it sets the
EVAL_* variables below and collects the results this file records.

EVAL_DATABASE_URL     the eval database (default: local Postgres, support_desk_eval)
EVAL_RESEED=1         rebuild the schema and corpus (automatic when the corpus is missing)
EVAL_FRESH=1          empty the Groq cache first: every model call is real
EVAL_IMPORT_CACHE=URL copy cached Groq replies from that database (e.g. the dev one) first
EVAL_RESULTS_JSON     where to write the raw results (cases, metrics)
"""

import json
import os
import sys
from collections.abc import Iterator
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from harness import (  # noqa: E402
    DEFAULT_DB,
    WORK,
    Db,
    EvalEnv,
    build_reviews_csv,
    needs_reseed,
    running_api,
    seed,
)

RESULTS: dict[str, Any] = {"cases": [], "metrics": {}}


@pytest.fixture(scope="session")
def env() -> Iterator[EvalEnv]:
    url = os.environ.get("EVAL_DATABASE_URL", DEFAULT_DB)
    stamp = os.environ.get("EVAL_STAMP") or datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = build_reviews_csv(WORK / "reviews_eval.csv")
    reseed = os.environ.get("EVAL_RESEED") == "1" or needs_reseed(Db(url))
    seed(url, csv_path, reseed=reseed, fresh=os.environ.get("EVAL_FRESH") == "1",
         import_cache_from=os.environ.get("EVAL_IMPORT_CACHE") or None)  # fmt: skip
    with running_api(url, csv_path, stamp) as e:
        RESULTS["meta"] = {"server_log": str(e.log_path), "mcp_log": str(e.mcp_log),
                           "started_at": e.started_at.isoformat(), "reseeded": reseed}  # fmt: skip
        yield e


@pytest.fixture(scope="session")
def record() -> Any:
    """record(name, value): a metric for the report, e.g. intent accuracy."""

    def put(name: str, value: Any) -> None:
        RESULTS["metrics"][name] = value

    return put


@pytest.fixture(scope="session")
def shared() -> dict[str, Any]:
    """State handed from one suite to a later one (e.g. the weekly run)."""
    return {}


LOOP_QUESTIONS = [
    "Why is my regular plan charging more than the direct plan?",
    "Why does my regular plan charge more than the direct plan?",
    "Why is the regular plan charging more than direct plan?",
    "why is my regular plan charging me more than the direct plan",
    "Why is my regular plan charging more than my direct plan?",
]


@pytest.fixture(scope="session")
def loop_questions(env: EvalEnv) -> list[dict[str, Any]]:
    """5 similar questions within 7 days: the cluster the next weekly run must surface."""
    return [env.chat(q)["answers"][0] for q in LOOP_QUESTIONS]


@pytest.fixture(scope="session")
def weekly_run(env: EvalEnv, loop_questions: list[dict[str, Any]]) -> dict[str, Any]:
    """One weekly run over the eval reviews, after the loop questions were asked. Its window
    ends today, as the cron job's does, so it includes the questions just asked."""
    return env.run_weekly(window_end=date.today().isoformat())


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        suite = Path(report.nodeid.split("::")[0]).stem.removeprefix("test_")
        message = ""
        if report.outcome == "failed":
            crash = getattr(report.longrepr, "reprcrash", None)
            message = crash.message if crash else str(report.longrepr)[-800:]
        elif report.outcome == "skipped" and isinstance(report.longrepr, tuple):
            message = report.longrepr[2]
        RESULTS["cases"].append({"suite": suite, "id": report.nodeid.split("::", 1)[-1],
                                 "outcome": report.outcome, "message": message[:1500],
                                 "seconds": round(report.duration, 1)})  # fmt: skip


def pytest_sessionfinish(session: pytest.Session) -> None:
    out = os.environ.get("EVAL_RESULTS_JSON")
    if out:
        Path(out).write_text(json.dumps(RESULTS, indent=2, default=str))
