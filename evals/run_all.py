"""Run every eval suite against a fresh eval database and write the report.

From backend/ (Postgres with pgvector running; EVAL_DATABASE_URL to use another database):

    uv run python ../evals/run_all.py                  # cached Groq replies are reused
    uv run python ../evals/run_all.py --fresh          # empty the Groq cache: every call is real
    uv run python ../evals/run_all.py --reseed         # rebuild the schema and the corpus first
    uv run python ../evals/run_all.py --import-cache   # first copy cached replies from .env's DB
    uv run python ../evals/run_all.py --label after -k multi   # name the report; pytest -k

Writes evals/results/<date>[-label].md and .json: pass and fail counts per suite, the failing
cases, intent accuracy and its confusion table, the not-covered rate, reviewer fail rates by
rule, and the number of Groq calls used (from the API's own log).
"""

import argparse
import collections
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

EVALS = Path(__file__).resolve().parent
sys.path.insert(0, str(EVALS))

from harness import BACKEND, DEFAULT_DB, WORK, Db  # noqa: E402

RESULTS = EVALS / "results"
# Order matters: grounding runs last so its PII searches cover what every suite stored.
SUITES = [
    "test_voice_intent.py",
    "test_multi_question.py",
    "test_loop_connection.py",
    "test_edit_correctness.py",
    "test_feasibility.py",
    "test_grounding_compliance.py",
]
TITLES = {
    "voice_intent": "Voice and intent",
    "multi_question": "Multi-question",
    "loop_connection": "Loop connection",
    "edit_correctness": "Edit correctness",
    "feasibility": "Feasibility",
    "grounding_compliance": "Grounding and compliance",
}


def groq_calls(log_path: Path) -> dict[str, Any]:
    real: collections.Counter[str] = collections.Counter()
    cached: collections.Counter[str] = collections.Counter()
    tasks: collections.Counter[str] = collections.Counter()
    whisper = fallback = rate_limited = 0
    for line in log_path.read_text(errors="replace").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = e.get("event")
        if event == "llm_call":
            (cached if e.get("cached") else real)[e.get("model", "?")] += 1
            if not e.get("cached"):
                tasks[e.get("task", "?")] += 1
        elif event == "voice_transcribed":
            whisper += 1
        elif event and "fallback" in event:
            fallback += 1
        elif event and "rate" in event and "limit" in event:
            rate_limited += 1
    return {"real_by_model": dict(real), "cached_by_model": dict(cached), "real_total": sum(real.values()),
            "cached_total": sum(cached.values()), "real_by_task": dict(tasks.most_common()),
            "whisper": whisper, "fallback_events": fallback, "rate_limit_events": rate_limited}  # fmt: skip


def reviewer_rates(db: Db) -> dict[str, Any]:
    rows = db.fetch("SELECT rule_results FROM review_result")
    totals: collections.Counter[str] = collections.Counter()
    fails: collections.Counter[str] = collections.Counter()
    for r in rows:
        results = (
            r["rule_results"]
            if isinstance(r["rule_results"], list)
            else json.loads(r["rule_results"])
        )
        for x in results:
            totals[x["rule"]] += 1
            fails[x["rule"]] += not x["passed"]
    return {
        "reviews": len(rows),
        "by_rule": {rule: {"failed": fails[rule], "checked": n, "rate": round(fails[rule] / n, 3)}
                    for rule, n in sorted(totals.items())},
    }  # fmt: skip


def relative(meta: dict[str, Any]) -> dict[str, Any]:
    """Paths relative to the repo, so a committed report holds no home directory."""
    root = str(EVALS.parent) + "/"
    return {k: v.replace(root, "") if isinstance(v, str) else v for k, v in meta.items()}


def write_report(
    raw: dict[str, Any], extra: dict[str, Any], name: str, args: argparse.Namespace
) -> Path:
    cases = raw.get("cases", [])
    by_suite: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for c in cases:
        by_suite[c["suite"]][c["outcome"]] += 1
    metrics = {**raw.get("metrics", {}), **extra}
    report = {"generated_at": datetime.now().isoformat(timespec="seconds"),
              "options": {"fresh": args.fresh, "reseed": args.reseed, "filter": args.k},
              "summary": {s: dict(c) for s, c in by_suite.items()},
              "metrics": metrics, "cases": cases, "meta": relative(raw.get("meta", {}))}  # fmt: skip
    RESULTS.mkdir(exist_ok=True)
    (RESULTS / f"{name}.json").write_text(json.dumps(report, indent=2, default=str) + "\n")

    lines = [f"# Eval results: {name}", "",
             f"Generated {report['generated_at']}. Options: fresh Groq cache {args.fresh}, "
             f"reseeded {raw.get('meta', {}).get('reseeded')}"
             + (f", filter `{args.k}`" if args.k else "") + ".", "",
             "| Suite | Passed | Failed | Skipped |", "|---|---|---|---|"]  # fmt: skip
    total = collections.Counter[str]()
    for s in TITLES:
        if s in by_suite:
            c = by_suite[s]
            total.update(c)
            lines.append(f"| {TITLES[s]} | {c['passed']} | {c['failed']} | {c['skipped']} |")
    lines.append(
        f"| **Total** | **{total['passed']}** | **{total['failed']}** | **{total['skipped']}** |"
    )

    ia = metrics.get("intent_accuracy")
    if ia:
        lines += ["", "## Intent accuracy", "",
                  f"{ia['correct']} of {ia['total']} ({ia['accuracy']:.0%}) on the labelled set.", ""]  # fmt: skip
        labels = ia["labels"]
        conf = ia["confusion"]
        lines += [
            "| expected \\ got | " + " | ".join(labels) + " |",
            "|---" * (len(labels) + 1) + "|",
        ]
        for want in labels:
            cells = [str(conf.get(f"{want}->{got}", "")) for got in labels]
            lines.append(f"| {want} | " + " | ".join(cells) + " |")

    lines += ["", "## Key numbers", ""]
    if "unrefused_advice_or_returns" in metrics:
        lines.append(
            f"- Unrefused advice or returns questions: {metrics['unrefused_advice_or_returns']}"
        )
    if "pii_scan" in metrics:
        p = metrics["pii_scan"]
        lines.append(
            f"- PII hits in stored text: {p['hits']} ({p['values_checked']} values checked)"
        )
    if "not_covered_set" in metrics:
        n = metrics["not_covered_set"]
        lines.append(f"- Not-covered set handled correctly: {n['correct']} of {n['total']}")
    if "not_covered_rate" in metrics:
        lines.append(
            f'- Share of all queue items answered "not covered": {metrics["not_covered_rate"]:.0%}'
        )
    if "voice_word_accuracy" in metrics:
        lines.append(
            f"- Voice transcription word accuracy: {metrics['voice_word_accuracy']['mean']:.0%}"
        )
    g = metrics.get("groq_calls", {})
    if g:
        lines.append(f"- Groq calls: {g['real_total']} real, {g['cached_total']} from the cache "
                     f"({g['real_by_model']}); Whisper: {g['whisper']}")  # fmt: skip
    rr = metrics.get("reviewer", {}).get("by_rule", {})
    if rr:
        lines += ["", "## Reviewer fail rates by rule", "", "| Rule | Failed | Checked | Rate |",
                  "|---|---|---|---|"]  # fmt: skip
        lines += [
            f"| {r} | {v['failed']} | {v['checked']} | {v['rate']:.1%} |" for r, v in rr.items()
        ]

    failed = [c for c in cases if c["outcome"] == "failed"]
    lines += ["", f"## Failing cases ({len(failed)})", ""]
    for c in failed:
        lines.append(
            f"- **{TITLES.get(c['suite'], c['suite'])}: `{c['id']}`**: {c['message'].splitlines()[0][:400]}"
        )
    if not failed:
        lines.append("None.")
    md = RESULTS / f"{name}.md"
    md.write_text("\n".join(lines) + "\n")
    return md


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the eval suites and write the report.")
    parser.add_argument("--fresh", action="store_true", help="empty the Groq cache first")
    parser.add_argument("--reseed", action="store_true", help="rebuild the schema and corpus")
    parser.add_argument(
        "--import-cache",
        action="store_true",
        help="copy cached Groq replies from the database in .env first",
    )
    parser.add_argument("--label", help="added to the report's file name, e.g. before or after")
    parser.add_argument("-k", help="only cases matching this pytest expression")
    parser.add_argument("-x", action="store_true", help="stop at the first failure")
    args = parser.parse_args()

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = datetime.now().strftime("%Y-%m-%d") + (f"-{args.label}" if args.label else "")
    raw_path = WORK / f"raw-{stamp}.json"
    WORK.mkdir(exist_ok=True)
    os.environ.update({"EVAL_STAMP": stamp, "EVAL_RESULTS_JSON": str(raw_path),
                       "EVAL_RESEED": "1" if args.reseed else "0",
                       "EVAL_FRESH": "1" if args.fresh else "0"})  # fmt: skip
    if args.import_cache:
        sys.path.insert(0, str(BACKEND))
        from app.config import get_settings

        os.environ["EVAL_IMPORT_CACHE"] = get_settings().database_url
    pytest_args = ["-c", str(EVALS / "pytest.ini"), "--rootdir", str(EVALS), "-q", "-rN",
                   *[str(EVALS / s) for s in SUITES]]  # fmt: skip
    if args.k:
        pytest_args += ["-k", args.k]
    if args.x:
        pytest_args.append("-x")
    code = pytest.main(pytest_args)

    raw = json.loads(raw_path.read_text()) if raw_path.exists() else {"cases": [], "metrics": {}}
    db = Db(os.environ.get("EVAL_DATABASE_URL", DEFAULT_DB))
    extra: dict[str, Any] = {}
    log = raw.get("meta", {}).get("server_log")
    if log and Path(log).exists():
        extra["groq_calls"] = groq_calls(Path(log))
    try:
        extra["reviewer"] = reviewer_rates(db)
    except Exception as exc:  # noqa: BLE001
        extra["reviewer"] = {"error": type(exc).__name__}
    md = write_report(raw, extra, name, args)
    print(f"\nReport: {md}")
    return int(code)


if __name__ == "__main__":
    raise SystemExit(main())
