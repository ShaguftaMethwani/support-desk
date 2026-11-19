"""Multi-question messages (architecture section 15).

Each expected part is found and answered on its own, with its own citation. Advice and returns
parts are refused while factual parts are answered. A scheme named once carries over to the
parts that don't name one. Parts beyond 5 are listed, not processed. PII in one part stays out
of every part. Rejecting one part leaves its siblings untouched. Some messages are spoken.
"""

from pathlib import Path
from typing import Any

import pytest
from checks import contains_raw, kind
from harness import DATASETS, EvalEnv, diff, load_jsonl, snapshot

MESSAGES = load_jsonl("multi_question.jsonl")
BY_ID = {m["id"]: m for m in MESSAGES}
SPOKEN = [c for c in load_jsonl("voice/clips.jsonl") if c["kind"] == "multi"]
RESULTS: dict[str, dict[str, Any]] = {}


def check(message: dict[str, Any], out: dict[str, Any]) -> list[str]:
    problems = []
    answers = out["answers"]
    want = message["parts"]
    if len(answers) != len(want):
        problems.append(f"{len(answers)} parts, expected {len(want)}: "
                        f"{[a['question'] for a in answers]}")  # fmt: skip
    dropped = len((out.get("dropped") or {}).get("questions") or [])
    if dropped != message.get("dropped", 0):
        problems.append(f"{dropped} parts listed as dropped, expected {message.get('dropped', 0)}")
    for i, (a, w) in enumerate(zip(answers, want, strict=False)):
        tag = f"part {i + 1}"
        if w["intent"] != "any" and a["intent"] != w["intent"]:
            problems.append(f"{tag}: intent {a['intent']}, expected {w['intent']}")
        got = kind(a)
        if w["expect"] != "any" and got != w["expect"]:
            problems.append(f"{tag}: {got}, expected {w['expect']}")
        draft = a.get("draft") or {}
        if got == "answered" and not draft.get("citation"):
            problems.append(f"{tag}: answered without its own citation")
        if "scheme" in w and a["slots"].get("scheme") != w["scheme"]:
            problems.append(f"{tag}: scheme {a['slots'].get('scheme')}, expected {w['scheme']}")
        if w.get("inherited") and not a["slots"].get("scheme_inherited"):
            problems.append(f"{tag}: scheme not carried over from another part")
    if len({a["id"] for a in answers}) != len(answers):
        problems.append("parts share a queue item")
    for i, a in enumerate(answers):
        text = a["question"] + " " + ((a.get("draft") or {}).get("reply") or "")
        if leaked := contains_raw(text, message.get("pii", [])):
            problems.append(f"part {i + 1} shows {leaked}")
        if "pii_part" in message and i != message["pii_part"] and "_REDACTED]" in a["question"]:
            problems.append(f"part {i + 1} carries another part's redaction")
    return problems


@pytest.mark.parametrize("message", MESSAGES, ids=[m["id"] for m in MESSAGES])
def test_typed(env: EvalEnv, message: dict[str, Any]) -> None:
    out = env.chat(message["text"])
    RESULTS[message["id"]] = out
    problems = check(message, out)
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("clip", SPOKEN, ids=[c["id"] for c in SPOKEN])
def test_spoken(env: EvalEnv, clip: dict[str, Any]) -> None:
    path = DATASETS / "voice" / "clips" / f"{clip['id']}.{clip['format']}"
    heard = env.voice(Path(path))
    out = env.chat(heard["transcript"], channel="voice")
    problems = check(BY_ID[clip["ref"]], out)
    assert not problems, f"heard {heard['transcript']!r}: " + "; ".join(problems)


def test_rejecting_one_part_leaves_its_siblings(env: EvalEnv, record: Any) -> None:
    out = RESULTS.get("m01") or env.chat(BY_ID["m01"]["text"])
    answers = out["answers"]
    if len(answers) < 2:
        pytest.fail("m01 was not split, so there is no sibling to compare")
    target, siblings = answers[1], answers[:1] + answers[2:]
    before = snapshot(env.db)
    r = env.client.post(f"/queue/{target['id']}/reject")
    assert r.status_code == 200, r.text
    d = diff(before, snapshot(env.db))
    record("multi_reject_diff", d.summary())
    assert d.tables() == {"queue_item"}, f"tables changed: {d.summary()}"
    assert d.changed["queue_item"] == {target["id"]}, f"rows changed: {d.changed['queue_item']}"
    for s in siblings:
        assert env.item(s["id"])["status"] == s["status"]
