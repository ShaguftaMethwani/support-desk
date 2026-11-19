"""Edit correctness (architecture section 15).

For each human decision (edit, approve, reject, at the queue and at the gate), every table is
snapshotted before and after, and the diff must hold exactly the rows that decision should
touch. Approving one action leaves the other pending. A refused request changes nothing.
"""

from typing import Any

import pytest
from harness import Diff, EvalEnv, diff, snapshot

QUESTIONS = [
    "What is the lock-in period of HDFC ELSS Tax Saver?",
    "What is the minimum SIP amount for HDFC Flexi Cap Fund?",
    "What is the benchmark of HDFC Mid Cap Fund?",
]
STATE: dict[str, Any] = {}


def rows(d: Diff) -> dict[str, dict[str, set[str]]]:
    return {"added": d.added, "removed": d.removed, "changed": d.changed}


def only(d: Diff, expected: dict[str, dict[str, int | set[str]]]) -> list[str]:
    """expected: {table: {"added": n, "changed": {ids}}}; everything else must be unchanged."""
    problems = []
    for table in d.tables() - set(expected):
        problems.append(f"{table} changed: {d.summary()[table]}")
    for table, want in expected.items():
        for what in ("added", "removed", "changed"):
            got = rows(d)[what].get(table, set())
            exp = want.get(what, 0)
            if isinstance(exp, set):
                if got != exp:
                    problems.append(f"{table} {what}: {sorted(got)} != {sorted(exp)}")
            elif len(got) != exp:
                problems.append(f"{table} {what}: {len(got)} rows, expected {exp}")
    return problems


@pytest.fixture(scope="module")
def items(env: EvalEnv) -> list[dict[str, Any]]:
    out = [env.chat(q)["answers"][0] for q in QUESTIONS]
    for a in out:
        if not (a.get("draft") or {}).get("approvable"):
            pytest.fail(f"setup: {a['question']!r} has no approvable draft ({a['status']})")
    return out


def test_edit_adds_a_version_and_its_review(
    env: EvalEnv, items: list[dict[str, Any]], record: Any
) -> None:
    item = items[0]
    reply = item["draft"]["reply"]
    first, _, rest = reply.partition("\n")
    edited = first.rstrip(".") + ", as stated in the scheme document.\n" + rest
    before = snapshot(env.db)
    r = env.client.put(f"/queue/{item['id']}/draft", json={"reply": edited})
    assert r.status_code == 200, r.text
    d = diff(before, snapshot(env.db))
    record("edit_diff", d.summary())
    status_changed = {item["id"]} if r.json()["draft"]["review_status"] != "passed" else set()
    problems = only(d, {"draft_version": {"added": 1}, "review_result": {"added": 1},
                        "queue_item": {"changed": status_changed}})  # fmt: skip
    assert not problems, "; ".join(problems)


def test_an_edit_that_fails_review_blocks_approval(
    env: EvalEnv, items: list[dict[str, Any]]
) -> None:
    item = items[1]
    reply = item["draft"]["reply"]
    first, _, rest = reply.partition("\n")
    bad = first + " You should definitely buy this fund now.\n" + rest
    r = env.client.put(f"/queue/{item['id']}/draft", json={"reply": bad})
    assert r.status_code == 200, r.text
    assert r.json()["draft"]["review_status"] == "blocked", r.json()["draft"]
    before = snapshot(env.db)
    refused = env.client.post(f"/queue/{item['id']}/approve")
    assert refused.status_code == 409, refused.text
    assert not diff(before, snapshot(env.db)).tables(), "a refused approval changed rows"


def test_approving_a_draft_only_creates_its_two_actions(env: EvalEnv, items: list[dict[str, Any]],
                                                        record: Any) -> None:  # fmt: skip
    item = items[0]
    before = snapshot(env.db)
    r = env.client.post(f"/queue/{item['id']}/approve")
    assert r.status_code == 200, r.text
    d = diff(before, snapshot(env.db))
    record("approve_draft_diff", d.summary())
    STATE["actions"] = r.json()["pending_action_ids"]
    problems = only(d, {"pending_action": {"added": 2}, "queue_item": {"changed": {item["id"]}}})
    assert not problems, "; ".join(problems)


def test_approving_one_action_leaves_the_other_pending(env: EvalEnv, record: Any) -> None:
    ids = STATE.get("actions")
    if not ids:
        pytest.fail("no actions from the previous step")
    actions = {a["id"]: a for a in env.pending_actions() if a["id"] in ids}
    doc = next(a for a in actions.values() if a["kind"] == "doc_append")
    email = next(a for a in actions.values() if a["kind"] == "email_draft")
    calls_before = len(env.mcp_calls())
    before = snapshot(env.db)
    r = env.approve_action(doc)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "executed", r.json()
    d = diff(before, snapshot(env.db))
    record("approve_action_diff", d.summary())
    problems = only(d, {"pending_action": {"changed": {doc["id"]}}, "approval": {"added": 1}})
    calls = env.mcp_calls()[calls_before:]
    if [c["tool"] for c in calls] != ["google_docs_append"]:
        problems.append(f"tool calls: {[c['tool'] for c in calls]}")
    still = next((a for a in env.pending_actions() if a["id"] == email["id"]), None)
    if not still or still["status"] != "pending":
        problems.append("the email draft action is no longer pending")
    STATE["email"] = email
    assert not problems, "; ".join(problems)


def test_a_changed_payload_hash_is_refused(env: EvalEnv) -> None:
    email = STATE.get("email")
    if not email:
        pytest.fail("no email action from the previous step")
    forged = {**email, "payload_hash": "0" * 64}
    calls_before = len(env.mcp_calls())
    before = snapshot(env.db)
    r = env.approve_action(forged)
    assert r.status_code in (400, 409), r.text
    assert not diff(before, snapshot(env.db)).tables(), "a refused approval changed rows"
    assert len(env.mcp_calls()) == calls_before, "a tool was called"


def test_rejecting_an_action_touches_only_it(env: EvalEnv) -> None:
    email = STATE.get("email")
    if not email:
        pytest.fail("no email action from the previous step")
    before = snapshot(env.db)
    r = env.client.post(f"/actions/{email['id']}/reject")
    assert r.status_code == 200, r.text
    problems = only(diff(before, snapshot(env.db)), {"pending_action": {"changed": {email["id"]}}})
    assert not problems, "; ".join(problems)


def test_rejecting_an_item_touches_only_it(env: EvalEnv, items: list[dict[str, Any]]) -> None:
    item = items[2]
    before = snapshot(env.db)
    r = env.client.post(f"/queue/{item['id']}/reject")
    assert r.status_code == 200, r.text
    problems = only(diff(before, snapshot(env.db)), {"queue_item": {"changed": {item["id"]}}})
    assert not problems, "; ".join(problems)
