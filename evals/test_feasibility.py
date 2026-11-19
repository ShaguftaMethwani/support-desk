"""Feasibility (architecture section 15 and the problem statement's checks).

- 20 real complaints from the reviews CSV: all three agents return structured output, they run
  in parallel, and each gets a draft with a citation or an explicit "not covered".
- Every queue item from every suite ends with a draft (nothing stuck or unavailable).
- The weekly run completes with no manual step; every theme traces to its review ids within a
  stated window; at most 5 themes; the pulse is at most 250 words; the explainer at most 6
  bullets; reply drafts at most 3 sentences.
"""

import re
from datetime import date, datetime
from typing import Any

import pytest
from checks import kind
from harness import EvalEnv, load_jsonl

COMPLAINTS = load_jsonl("complaints_20.jsonl")
AGENTS = {"facts", "pattern", "compliance"}


@pytest.fixture(scope="module")
def complaints(env: EvalEnv) -> list[dict[str, Any]]:
    r = env.client.post(
        "/queue/import-csv", json={"review_ids": [c["review_id"] for c in COMPLAINTS]}
    )
    r.raise_for_status()
    return [env.item(i["id"]) for i in r.json()["items"]]


@pytest.mark.parametrize("n", range(len(COMPLAINTS)), ids=[c["review_id"][:20] for c in COMPLAINTS])
def test_complaint(complaints: list[dict[str, Any]], n: int) -> None:
    if n >= len(complaints):
        pytest.fail(f"only {len(complaints)} queue items for {len(COMPLAINTS)} complaints")
    item = complaints[n]
    problems = []
    k = kind(item)
    if k not in ("answered", "not_covered", "refused", "clarification"):
        problems.append(f"ended as {k}")
    draft = item.get("draft") or {}
    if k in ("answered", "refused") and not draft.get("citation"):
        problems.append("no citation")
    runs = {r["agent"]: r for r in item["agent_runs"]}
    # Refusals and clarifications skip the Facts Agent by design.
    needed = AGENTS if k not in ("refused", "clarification") else AGENTS - {"facts"}
    for agent in needed:
        r = runs.get(agent)
        if not r or r["status"] not in ("ok", "cached") or not isinstance(r["output"], dict):
            problems.append(f"{agent}: no structured output")
    # In parallel: every agent starts before any other has finished (a run that takes 0 ms,
    # like rule-only Compliance, starts and ends at once and is left out of the comparison).
    timed = [(datetime.fromisoformat(r["started_at"]), datetime.fromisoformat(r["finished_at"]))
             for a, r in runs.items() if a in needed and r["finished_at"]]  # fmt: skip
    slow = [(s, e) for s, e in timed if (e - s).total_seconds() > 0.05]
    if len(slow) >= 2 and max(s for s, _ in slow) >= min(e for _, e in slow):
        problems.append("agents did not overlap (not run in parallel)")
    assert not problems, "; ".join(problems)


def test_every_queue_item_has_a_draft(env: EvalEnv, record: Any) -> None:
    rows = env.db.fetch(
        "SELECT q.id, q.status, q.text_redacted, d.source_type FROM queue_item q LEFT JOIN LATERAL "
        "(SELECT source_type FROM draft_version WHERE item_id = q.id ORDER BY version DESC LIMIT 1)"
        " d ON true"
    )
    by_type: dict[str, int] = {}
    for r in rows:
        by_type[r["source_type"] or "none"] = by_type.get(r["source_type"] or "none", 0) + 1
    record("drafts_by_source_type", by_type)
    record("not_covered_rate", round(by_type.get("not_covered", 0) / max(len(rows), 1), 3))
    bad = [f"{r['text_redacted'][:50]!r} ({r['status']})" for r in rows
           if r["source_type"] is None or r["status"] in ("new", "drafting", "unavailable")]  # fmt: skip
    assert rows, "no queue items"
    assert not bad, f"{len(bad)} of {len(rows)} items without a draft: {bad[:10]}"


def test_reply_drafts_stay_within_3_sentences(env: EvalEnv) -> None:
    rows = env.db.fetch(
        "SELECT r.rule_results, d.review_status FROM review_result r "
        "JOIN draft_version d ON d.id = r.draft_version_id WHERE d.review_status = 'passed'"
    )
    import json

    over = []
    for r in rows:
        results = (
            r["rule_results"]
            if isinstance(r["rule_results"], list)
            else json.loads(r["rule_results"])
        )
        cap = next((x for x in results if x["rule"] == "length_caps"), None)
        if cap and not cap["passed"]:
            over.append(cap["detail"])
    assert not over, f"passed drafts over the cap: {over[:5]}"


def test_the_weekly_run_completes_unattended(weekly_run: dict[str, Any]) -> None:
    assert weekly_run["status"] == "awaiting_approval", (
        f"{weekly_run['status']}: {weekly_run['error']}"
    )
    assert set(weekly_run["stages"].values()) == {"done"}, weekly_run["stages"]
    assert {a["kind"] for a in weekly_run["actions"]} >= {"doc_append", "email_draft"}


def test_themes_trace_to_review_ids_within_a_window(
    env: EvalEnv, weekly_run: dict[str, Any]
) -> None:
    problems = []
    review_themes = [t for t in weekly_run["themes"] if not t["from_support_queue"]]
    if not 1 <= len(review_themes) <= 5:
        problems.append(f"{len(review_themes)} review themes")
    for t in weekly_run["themes"]:
        ids = t["question_ids"] if t["from_support_queue"] else t["review_ids"]
        if t["count"] != len(ids) or not ids:
            problems.append(f"{t['label']}: count {t['count']} vs {len(ids)} ids")
        if not (t["window"].get("start") and t["window"].get("end")):
            problems.append(f"{t['label']}: no window")
            continue
        start, end = (
            date.fromisoformat(t["window"]["start"]),
            date.fromisoformat(t["window"]["end"]),
        )
        if t["from_support_queue"]:
            found = env.db.fetch(
                "SELECT created_at FROM queue_item WHERE id = ANY($1::uuid[])", ids
            )
            dates = [r["created_at"].date() for r in found]
        else:
            found = env.db.fetch("SELECT date FROM review WHERE review_id = ANY($1::text[])", ids)
            dates = [r["date"] for r in found]
        if len(found) != len(ids):
            problems.append(f"{t['label']}: {len(ids) - len(found)} ids not found")
        if any(not start <= d <= end for d in dates):
            problems.append(f"{t['label']}: items outside {start}..{end}")
    assert not problems, "; ".join(problems)


def test_pulse_is_at_most_250_words(weekly_run: dict[str, Any], record: Any) -> None:
    pulse = weekly_run.get("pulse")
    assert pulse, "no pulse"
    # Words carry a letter or digit: bullet dashes and lone brackets are not words.
    words = len([t for t in pulse["text"].split() if re.search(r"[A-Za-z0-9]", t)])
    record("pulse_words", {"reported": pulse["word_count"], "counted": words})
    assert pulse["word_count"] <= 250 and words <= 250, f"{pulse['word_count']} / {words} words"


def test_explainer_is_at_most_6_bullets(weekly_run: dict[str, Any]) -> None:
    explainer = weekly_run.get("explainer")
    if explainer is None:
        pytest.skip("no fee confusion found, so no explainer")
    assert 1 <= len(explainer["bullets"]) <= 6, f"{len(explainer['bullets'])} bullets"
    assert explainer["source_urls"] and explainer["last_checked"]


# Problem statement 8.5, branch coverage: one complaint the corpus answers cleanly, one it
# can't answer, one matching a recurring theme, and one containing PII. Which agents fired on
# each is recorded, and the PII must be caught before anything reaches a draft.
BRANCHES = [
    ("answered", "I was charged an exit load when I redeemed HDFC Liquid Fund units after 2 "
                 "days. What is the exit load of HDFC Liquid Fund?"),
    ("not_covered", "The app crashed while I was adding money and now my balance shows zero."),
    ("recurring", "The app keeps crashing and the verification fails every time I try to log "
                  "in, I cannot even see my investments."),
    ("pii", "My PAN is ABCPE1234F and my SIP in HDFC Mid Cap Fund failed twice this month."),
]  # fmt: skip


@pytest.mark.parametrize(("branch", "text"), BRANCHES, ids=[b for b, _ in BRANCHES])
def test_branch_coverage(env: EvalEnv, record: Any, branch: str, text: str) -> None:
    answer = env.chat(text)["answers"][0]
    item = env.item(answer["id"])
    fired = {r["agent"]: r["status"] for r in item["agent_runs"]}
    pattern = next((r["output"] for r in item["agent_runs"] if r["agent"] == "pattern"), {}) or {}
    compliance = next((r["output"] for r in item["agent_runs"] if r["agent"] == "compliance"),
                      {}) or {}  # fmt: skip
    review = (item["versions"][-1].get("review") or []) if item["versions"] else []
    record(f"branch_{branch}", {
        "intent": item["intent"], "answer": kind(item), "agents_fired": fired,
        "pattern": pattern.get("label"), "pii_flags": [f["detail"] for f in
                                                       compliance.get("incoming_flags", [])
                                                       if f["type"] == "pii"],
        "reviewer": {r["rule"]: r["passed"] for r in review},
    })  # fmt: skip
    problems = []
    if not {"pattern", "compliance"} <= set(fired):
        problems.append(f"agents fired: {fired}")
    if branch == "answered" and kind(item) != "answered":
        problems.append(f"expected a cited answer, got {kind(item)}")
    if branch == "not_covered" and kind(item) != "not_covered":
        problems.append(f"expected not covered, got {kind(item)}")
    if branch == "recurring" and pattern.get("label") not in ("recurring", "rising"):
        problems.append(f"pattern {pattern.get('label')}, expected recurring or rising")
    if branch == "pii":
        if "ABCPE1234F" in item["question"]:
            problems.append("the PAN reached the queue")
        if not any(f["type"] == "pii" for f in compliance.get("incoming_flags", [])):
            problems.append("Compliance did not flag the PII")
        if not all(r["passed"] for r in review if r["rule"] == "pii_guard"):
            problems.append("the reviewer found PII in the draft")
    assert not problems, "; ".join(problems)
