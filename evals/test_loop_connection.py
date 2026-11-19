"""Loop connection, both directions (architecture sections 9 and 15).

Response loop -> weekly loop: 5 similar questions within 7 days form a cluster, and the next
weekly run lists it in the pulse with its count, window and queue item ids.
Weekly loop -> response loop: approving the run's promote_canonical action makes its fee
explainer canonical; a related question is then answered with it, word for word.
"""

import re
from typing import Any

import pytest
from harness import EvalEnv

RELATED = "Why is my regular plan charging more than the direct plan?"
UNRELATED = "What is the exit load of HDFC Liquid Fund?"


def test_the_questions_form_one_surfaced_cluster(
    env: EvalEnv, loop_questions: list[dict[str, Any]]
) -> None:
    ids = [q["id"] for q in loop_questions]
    rows = env.db.fetch(
        "SELECT q.cluster_id, c.surfaced FROM queue_item q LEFT JOIN question_cluster c "
        "ON c.id = q.cluster_id WHERE q.id = ANY($1::uuid[])", ids,
    )  # fmt: skip
    clusters = {str(r["cluster_id"]) for r in rows}
    assert len(clusters) == 1 and None not in {r["cluster_id"] for r in rows}, (
        f"clusters: {clusters}"
    )
    assert all(r["surfaced"] for r in rows), "cluster not surfaced after 5 questions"


def test_the_cluster_is_in_the_pulse(env: EvalEnv, weekly_run: dict[str, Any],
                                     loop_questions: list[dict[str, Any]]) -> None:  # fmt: skip
    ids = {q["id"] for q in loop_questions}
    support = [t for t in weekly_run["themes"] if t["from_support_queue"]]
    assert support, "no theme from the support queue"
    theme = next((t for t in support if ids <= set(t["question_ids"])), None)
    assert theme, f"no support theme holds the 5 questions: {[t['question_ids'] for t in support]}"
    assert theme["count"] == len(theme["question_ids"]) >= 5
    assert theme["window"]["start"] and theme["window"]["end"]
    pulse = (weekly_run.get("pulse") or {}).get("text", "")
    assert theme["label"] in pulse, "the pulse does not name the cluster"
    doc = next(a for a in env.pending_actions()
               if a["kind"] == "doc_append" and a["origin_id"] == weekly_run["run_id"])  # fmt: skip
    listed = {qid for c in doc["payload"].get("support_queue", []) for qid in c["question_ids"]}
    assert ids <= listed, "the doc entry does not list the cluster's queue items"


def test_a_promoted_explainer_answers_related_questions(env: EvalEnv, weekly_run: dict[str, Any],
                                                       record: Any) -> None:  # fmt: skip
    promote = [a for a in env.pending_actions()
               if a["kind"] == "promote_canonical" and a["origin_id"] == weekly_run["run_id"]]  # fmt: skip
    if not promote:
        pytest.fail("the weekly run made no promote_canonical action (no fee explainer)")
    action = promote[0]
    r = env.approve_action(action)
    assert r.status_code == 200, r.text
    active = [e for e in env.client.get("/explainers/canonical").json()
              if e["active"] and e["fee_type"] == action["payload"]["fee_type"]]  # fmt: skip
    assert len(active) == 1, f"active explainers: {active}"
    explainer = active[0]
    before = explainer["reuse_count"]

    answer = env.chat(RELATED)["answers"][0]
    draft = answer["draft"] or {}
    problems = []
    if draft.get("source_type") != "canonical_explainer":
        problems.append(f"source_type {draft.get('source_type')}")
    if draft.get("canonical_explainer_id") != explainer["id"]:
        problems.append("not linked to the active explainer")
    body = draft.get("reply", "").split("\nSource:")[0].strip()
    bullets = " ".join(explainer["bullets"])
    sentences = [s for s in re.split(r"(?<=\.)\s+", body) if s]
    if not sentences or any(s not in bullets for s in sentences):
        problems.append(f"the reply is not the explainer's own words: {body!r}")
    after = next(
        e for e in env.client.get("/explainers/canonical").json() if e["id"] == explainer["id"]
    )
    if after["reuse_count"] != before + 1:
        problems.append(f"reuse count {before} -> {after['reuse_count']}")

    other = env.chat(UNRELATED)["answers"][0]
    if (other.get("draft") or {}).get("source_type") == "canonical_explainer":
        problems.append("an unrelated scheme question was answered from the explainer")
    record("loop_canonical", {"fee_type": explainer["fee_type"], "reuse_before": before,
                              "reuse_after": after["reuse_count"],
                              "related_source_type": draft.get("source_type"),
                              "unrelated_source_type": (other.get("draft") or {}).get("source_type")})  # fmt: skip
    assert not problems, "; ".join(problems)
