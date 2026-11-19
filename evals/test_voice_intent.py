"""Voice and intent (architecture section 15).

- Intent accuracy on the labelled set, typed through POST /chat/message, with a confusion table.
- Voice parity: each of the 10 spoken questions, transcribed and confirmed, is handled like its
  typed version (same intent, same kind of answer, same source).
- Spoken PII: the 3 PII clips come back redacted, before the user even sees the transcript.
"""

import collections
from pathlib import Path
from typing import Any

import pytest
from checks import contains_raw, kind, word_accuracy
from harness import DATASETS, EvalEnv, load_jsonl

INTENTS = load_jsonl("intents_labelled.jsonl")
CLIPS = load_jsonl("voice/clips.jsonl")
PARITY = [c for c in CLIPS if c["kind"] == "parity"]
SPOKEN_PII = [c for c in CLIPS if c["kind"] == "pii"]
# The made-up values the PII clips say aloud.
PII_VALUES = {"p01": ["ABCPE1234F"], "p02": ["50100234567891"], "p03": ["9876543210"]}
MIN_ACCURACY = 0.85

TYPED: dict[str, dict[str, Any]] = {}  # label id -> the typed answer


def clip_path(clip: dict[str, Any]) -> Path:
    return DATASETS / "voice" / "clips" / f"{clip['id']}.{clip['format']}"


@pytest.mark.parametrize("row", INTENTS, ids=[r["id"] for r in INTENTS])
def test_intent(env: EvalEnv, row: dict[str, Any]) -> None:
    out = env.chat(row["text"])
    # A labelled row that really holds two questions ("what is X and what is Y?") may be split;
    # it counts as correct when every part gets the label.
    answers = out["answers"]
    wrong = [a["intent"] for a in answers if a["intent"] != row["intent"]]
    TYPED[row["id"]] = {**answers[0], "intent": wrong[0] if wrong else row["intent"]}
    assert not wrong, f"expected {row['intent']}, got {[a['intent'] for a in answers]}"


def test_intent_accuracy(record: Any) -> None:
    if not TYPED:
        pytest.skip("no intent cases ran")
    confusion: collections.Counter[tuple[str, str]] = collections.Counter()
    for row in INTENTS:
        if row["id"] in TYPED:
            confusion[(row["intent"], TYPED[row["id"]]["intent"] or "none")] += 1
    total = sum(confusion.values())
    correct = sum(n for (want, got), n in confusion.items() if want == got)
    labels = sorted({k for pair in confusion for k in pair})
    record("intent_accuracy", {"correct": correct, "total": total, "accuracy": round(correct / total, 3),
                               "labels": labels,
                               "confusion": {f"{w}->{g}": n for (w, g), n in sorted(confusion.items())}})  # fmt: skip
    assert correct / total >= MIN_ACCURACY, f"accuracy {correct}/{total} below {MIN_ACCURACY}"


VOICE: dict[str, dict[str, Any]] = {}


@pytest.mark.parametrize("clip", PARITY, ids=[c["id"] for c in PARITY])
def test_voice_parity(env: EvalEnv, clip: dict[str, Any]) -> None:
    heard = env.voice(clip_path(clip))
    accuracy = word_accuracy(clip["text"], heard["transcript"])
    out = env.chat(heard["transcript"], channel="voice")
    spoken = out["answers"]
    typed = TYPED.get(clip["label_id"]) or env.chat(clip["text"])["answers"][0]
    VOICE[clip["id"]] = {"word_accuracy": round(accuracy, 2), "transcript": heard["transcript"]}
    problems = []
    if accuracy < 0.8:
        problems.append(f"word accuracy {accuracy:.2f}: heard {heard['transcript']!r}")
    if len(spoken) != 1:
        problems.append(f"spoken version split into {len(spoken)} parts")
    else:
        s = spoken[0]
        if s["intent"] != typed["intent"]:
            problems.append(f"intent {s['intent']} spoken vs {typed['intent']} typed")
        if kind(s) != kind(typed):
            problems.append(f"answer {kind(s)} spoken vs {kind(typed)} typed")
        sc = ((s.get("draft") or {}).get("citation") or {}).get("source_key")
        tc = ((typed.get("draft") or {}).get("citation") or {}).get("source_key")
        if sc != tc:
            problems.append(f"source {sc} spoken vs {tc} typed")
    assert not problems, "; ".join(problems)


@pytest.mark.parametrize("clip", SPOKEN_PII, ids=[c["id"] for c in SPOKEN_PII])
def test_spoken_pii(env: EvalEnv, clip: dict[str, Any]) -> None:
    heard = env.voice(clip_path(clip))
    problems = []
    if leaked := contains_raw(heard["transcript"], PII_VALUES[clip["id"]]):
        problems.append(f"transcript shows {leaked}")
    if not set(clip["pii"]) <= set(heard["pii_flags"]):
        problems.append(f"flags {heard['pii_flags']}, expected {clip['pii']}")
    out = env.chat(heard["transcript"], channel="voice")
    for a in out["answers"]:
        if leaked := contains_raw(a["question"] + " " + ((a.get("draft") or {}).get("reply") or ""),
                                  PII_VALUES[clip["id"]]):  # fmt: skip
            problems.append(f"answer shows {leaked}")
    assert not problems, "; ".join(problems)


def test_voice_summary(record: Any) -> None:
    if not VOICE:
        pytest.skip("no voice cases ran")
    acc = [v["word_accuracy"] for v in VOICE.values()]
    record("voice_word_accuracy", {"clips": len(acc), "mean": round(sum(acc) / len(acc), 3),
                                   "per_clip": VOICE})  # fmt: skip
