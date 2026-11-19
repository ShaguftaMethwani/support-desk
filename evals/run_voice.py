"""Voice clips through the running API (Phase 7 exit check; Phase 9 adds intent parity).

Run from backend/ with the API running locally:
    uv run python ../evals/run_voice.py [--base http://localhost:8000] [--ask]

Each clip in evals/datasets/voice/clips.jsonl is posted to POST /chat/voice. For each one:
- **Word accuracy:** the transcript is compared with the expected words (case, punctuation and
  number formatting ignored).
- **PII clips:** each must come back with its PII type flagged, its redaction token in place,
  and none of the spoken digits or letters left in the text.

With --ask, the PII clips' confirmed transcripts also go to POST /chat/message with channel
"voice", so a database search can confirm that nothing unredacted was stored.

Writes evals/results/voice_transcription.json. Uses the real Groq Whisper API: 13 requests.
"""

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "evals" / "datasets" / "voice"
RESULTS = ROOT / "evals" / "results" / "voice_transcription.json"
TYPES = {"wav": "audio/wav", "m4a": "audio/mp4"}
# The made-up values spoken in the PII clips, as digits and letters only.
SPOKEN_PII = ("ABCPE1234F", "50100234567891", "9876543210")
_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}  # fmt: skip


def words(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", text.lower().replace("-", " "))
    return [_NUMBER_WORDS.get(t, t) for t in tokens]


def word_accuracy(expected: str, heard: str) -> float:
    """1 - word error rate (Levenshtein over words), floored at 0."""
    a, b = words(expected), words(heard)
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return max(0.0, 1 - prev[-1] / max(len(a), 1))


def post(url: str, data: bytes, content_type: str) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=data, headers={"content-type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--ask", action="store_true")
    args = parser.parse_args()
    clips = [json.loads(line) for line in (DATA / "clips.jsonl").read_text().splitlines()]
    results, failures = [], 0
    for clip in clips:
        audio = (DATA / "clips" / f"{clip['id']}.{clip['format']}").read_bytes()
        status, body = post(f"{args.base}/chat/voice", audio, TYPES[clip["format"]])
        transcript = body.get("transcript", "")
        row = {
            "id": clip["id"], "kind": clip["kind"], "status": status,
            "expected": clip["text"], "transcript": transcript,
            "word_accuracy": round(word_accuracy(clip["text"], transcript), 3),
            "pii_flags": body.get("pii_flags", []),
            "suggestions": body.get("suggestions", []),
            "duration_seconds": body.get("duration_seconds"),
        }  # fmt: skip
        ok = status == 200
        if clip["kind"] == "pii":
            squashed = re.sub(r"[^A-Za-z0-9]", "", transcript).upper()
            leaked = [v for v in SPOKEN_PII if v in squashed]
            tokens = all(f"[{t.upper()}_REDACTED]" in transcript for t in clip["pii"])
            row["pii_ok"] = (
                ok and set(clip["pii"]) <= set(row["pii_flags"]) and tokens and not leaked
            )
            ok = row["pii_ok"]
            if args.ask and status == 200:
                s, reply = post(f"{args.base}/chat/message",
                                json.dumps({"text": transcript, "channel": "voice"}).encode(),
                                "application/json")  # fmt: skip
                row["asked"] = {"status": s, "items": [a["id"] for a in reply.get("answers", [])]}
        failures += not ok
        results.append(row)
        mark = "ok " if ok else "FAIL"
        print(f"{mark} {clip['id']} {status} acc={row['word_accuracy']:.2f} "
              f"flags={row['pii_flags']} sugg={[s['suggestion'] for s in row['suggestions']]}")  # fmt: skip
        print(f"     heard: {transcript}")
    parity = [r for r in results if r["kind"] == "parity"]
    summary = {
        "clips": len(results),
        "parity_mean_word_accuracy": round(
            sum(r["word_accuracy"] for r in parity) / len(parity), 3
        ),
        "parity_exact": sum(r["word_accuracy"] == 1.0 for r in parity),
        "pii_ok": sum(bool(r.get("pii_ok")) for r in results if r["kind"] == "pii"),
        "failures": failures,
    }
    RESULTS.write_text(json.dumps({"summary": summary, "results": results}, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
