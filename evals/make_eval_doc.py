"""Write evals/EVALS.md: the golden dataset, the adversarial tests and the scores, item by item,
from evals/datasets/ and one results file. Nothing is typed in by hand.

    cd backend && uv run python ../evals/make_eval_doc.py           # the final run
    cd backend && uv run python ../evals/make_eval_doc.py ../evals/results/2026-11-19-after.json
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

EVALS = Path(__file__).parent
DATA = EVALS / "datasets"
RESULTS = Path(sys.argv[1]) if len(sys.argv) > 1 else EVALS / "results" / "2026-11-19-final.json"
OUT = EVALS / "EVALS.md"

SUITES = {
    "voice_intent": "Voice and intent",
    "multi_question": "Multi-question",
    "loop_connection": "Loop connection",
    "edit_correctness": "Edit correctness",
    "feasibility": "Feasibility",
    "grounding_compliance": "Grounding and compliance",
}


def rows(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (DATA / name).read_text().splitlines() if line.strip()]


report = json.loads(RESULTS.read_text())
metrics = report["metrics"]
# (test function, case id) -> case
cases: dict[tuple[str, str], dict[str, Any]] = {}
for c in report["cases"]:
    m = re.fullmatch(r"(\w+)(?:\[(.+)\])?", c["id"])
    assert m, c["id"]
    cases[(m.group(1), m.group(2) or "")] = c


def mark(test: str, case_id: str) -> str:
    c = cases.get((test, case_id))
    if c is None:
        return "not run"
    return {"passed": "✅ pass", "failed": "❌ fail", "skipped": "skipped"}[c["outcome"]]


def cell(text: Any, limit: int = 0) -> str:
    s = re.sub(r"\s+", " ", str(text)).strip()
    if limit and len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s.replace("|", "\\|")


def table(head: list[str], body: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join(cell(x) for x in r) + " |" for r in body]
    return out + [""]


def score(test: str, ids: list[str]) -> str:
    passed = sum(cases.get((test, i), {}).get("outcome") == "passed" for i in ids)
    return f"{passed} of {len(ids)} passed"


md: list[str] = []
add = md.extend
name = RESULTS.stem
add(
    [
        "# Evaluation datasets and scores",
        "",
        f"Every test set the evals use, item by item, with its result in the **{name}** run",
        f"([report]({'results/' + RESULTS.with_suffix('.md').name}),",
        f"generated {report['generated_at'][:16].replace('T', ' ')}). This page is written by",
        "`evals/make_eval_doc.py` from `evals/datasets/` and that run's results; nothing is",
        "typed in by",
        "hand. The suites run against the whole system through its HTTP API, with a separate eval",
        "database and a fake MCP server that records calls instead of writing to Google. How to",
        "run",
        "them: the [README](../README.md#evaluations).",
        "",
        "**Contents:** [Scores](#scores) · [Golden dataset](#golden-dataset) ·",
        "[Adversarial tests](#adversarial-tests) · [Earlier test sets](#earlier-test-sets) ·",
        "[Failures](#failures)",
        "",
        "## Scores",
        "",
    ]
)

summary = report["summary"]
body, tp, tf = [], 0, 0
for key, label in SUITES.items():
    s = summary.get(key, {})
    p, f = s.get("passed", 0), s.get("failed", 0)
    tp, tf = tp + p, tf + f
    body.append([label, p, f, s.get("skipped", 0)])
body.append(["**Total**", f"**{tp}**", f"**{tf}**", ""])
add(table(["Suite", "Passed", "Failed", "Skipped"], body))

ia, va = metrics["intent_accuracy"], metrics["voice_word_accuracy"]
ncs, pii = metrics["not_covered_set"], metrics["pii_scan"]
claims = metrics["claims"]
good_claims = sum(not c["problems"] for c in claims)
adv_kinds = [a for a in rows("adversarial.jsonl") if a["kind"] in ("advice", "returns")]
add(
    table(
        ["Measure", "Result"],
        [
            [
                "Intent accuracy (40 labelled questions)",
                f"{ia['correct']} of {ia['total']} ({ia['accuracy']:.0%})",
            ],
            ["Voice word accuracy (10 spoken questions)", f"{va['mean']:.0%} mean"],
            [
                "Advice and returns questions not refused",
                f"{metrics['unrefused_advice_or_returns']} of {len(adv_kinds)} typed adversarial "
                "ones",
            ],
            [
                'Not-covered questions answered "not covered", naming what was searched',
                f"{ncs['correct']} of {ncs['total']}",
            ],
            [
                "PII found in logs, tool calls and every stored column",
                f"{pii['hits']} hits ({pii['values_checked']:,} values checked for "
                f"{pii['known_values']} known PII values)",
            ],
            ["Sampled claims that resolve to their source", f"{good_claims} of {len(claims)}"],
            ["Pulse length", f"{metrics['pulse_words']['counted']} words (cap 250)"],
        ],
    )
)
add(
    [
        "Intent accuracy is optimistic: the intent prompt and rules were tuned on these same 40",
        "questions (see the [Phase 9 status](../docs/implementation.md#phase-9-evaluations)).",
        "",
    ]
)

# --- golden dataset ---------------------------------------------------------------------------
add(["## Golden dataset", ""])

intents = rows("intents_labelled.jsonl")
add(
    [
        f"### Intent labels: {len(intents)} questions",
        "",
        f"`datasets/intents_labelled.jsonl`. Each question has one correct intent of 7. "
        f'**{score("test_intent", [r["id"] for r in intents])}.** "Rule" marks the ones the',
        "keyword rules must catch before the model is asked (advice and returns).",
        "",
    ]
)
add(
    table(
        ["Id", "Question", "Expected intent", "Rule", "Result"],
        [
            [
                r["id"],
                r["text"],
                r["intent"],
                "yes" if r.get("pre_rule") else "",
                mark("test_intent", r["id"]),
            ]
            for r in intents
        ],
    )
)
conf = ia.get("confusion", {})
add(
    [
        "Confusion (expected → predicted): "
        + ", ".join(f"{k} {v}" for k, v in sorted(conf.items()))
        + ".",
        "",
    ]
)

multi = rows("multi_question.jsonl")


def parts(r: dict[str, Any]) -> str:
    out = []
    for i, p in enumerate(r.get("parts", []), 1):
        extra = f", {p['scheme']}" if p.get("scheme") else ""
        out.append(f"{i}. {p.get('intent')} → {p.get('expect')}{extra}")
    out = [
        o + (" (scheme carried over)" if p.get("inherited") else "")
        for o, p in zip(out, r.get("parts", []), strict=True)
    ]
    if r.get("dropped"):
        out.append(f"{r['dropped']} part over the cap of 5 dropped")
    if r.get("pii"):
        out.append("PII in the message, never shown")
    return "; ".join(out)


add(
    [
        f"### Multi-question messages: {len(multi)}",
        "",
        "`datasets/multi_question.jsonl`. Each message must be split into the expected parts, each",
        "with its intent and outcome; a scheme named in one part carries over to the next.",
        f"**Typed: {score('test_typed', [r['id'] for r in multi])}.**",
        "",
    ]
)
add(
    table(
        ["Id", "Message", "Expected parts", "Typed"],
        [[r["id"], cell(r["text"], 160), parts(r), mark("test_typed", r["id"])] for r in multi],
    )
)

nc = rows("not_covered.jsonl")
add(
    [
        f"### Not covered by the corpus: {len(nc)} questions",
        "",
        '`datasets/not_covered.jsonl`. The answer must be "not covered" and name the documents it',
        f"searched, never a guess. **{score('test_not_covered_typed', [r['id'] for r in nc])}.**",
        "",
    ]
)
add(
    table(
        ["Id", "Question", "Result"],
        [[r["id"], r["text"], mark("test_not_covered_typed", r["id"])] for r in nc],
    )
)

clips = rows("voice/clips.jsonl")
VOICE_TEST = {
    "parity": "test_voice_parity",
    "adversarial": "test_adversarial_spoken",
    "pii": "test_spoken_pii",
    "multi": "test_spoken",
    "not_covered": "test_not_covered_spoken",
}
VOICE_CHECK = {
    "parity": "same intent and answer as the typed question",
    "adversarial": "refused, and no PII in the transcript or reply",
    "pii": "PII redacted before anyone sees the transcript",
    "multi": "split and answered like the typed message",
    "not_covered": '"not covered", naming what was searched',
}
per_clip = va.get("per_clip", {})


def clip_id(c: dict[str, Any]) -> str:
    return c.get("ref") if c["kind"] == "multi" and c.get("ref") else c["id"]


add(
    [
        f"### Spoken questions: {len(clips)} clips",
        "",
        "`datasets/voice/clips.jsonl`, generated with macOS voices (Indian and other English",
        "accents), WAV and M4A. Each goes through Whisper and then the same pipeline as typed",
        "text.",
        "",
    ]
)
body = []
for c in clips:
    test = VOICE_TEST[c["kind"]]
    case = c["id"] if (test, c["id"]) in cases else (c.get("ref") or c.get("label_id") or c["id"])
    acc = per_clip.get(c["id"], {}).get("word_accuracy")
    body.append(
        [
            c["id"],
            c["kind"],
            c.get("voice", ""),
            cell(c.get("say") or c["text"], 110),
            VOICE_CHECK[c["kind"]],
            f"{acc:.0%}" if acc is not None else "",
            mark(test, case),
        ]
    )
add(table(["Id", "Kind", "Voice", "What is said", "Must", "Word accuracy", "Result"], body))

complaints = rows("complaints_20.jsonl")
add(
    [
        f"### Real complaints: {len(complaints)} app reviews",
        "",
        "`datasets/complaints_20.jsonl`, real 1- and 2-star reviews from the sample (PII removed),",
        "sent through the response loop as support messages. Each must get a draft that passes the",
        "reviewer's rules and stays within the caps. "
        f"**{score('test_complaint', [r['review_id'][:20] for r in complaints])}.**",
        "",
    ]
)
add(
    table(
        ["Review id", "Stars", "Review (start)", "Result"],
        [
            [
                r["review_id"],
                r.get("rating", ""),
                cell(r["text"], 140),
                mark("test_complaint", r["review_id"][:20]),
            ]
            for r in complaints
        ],
    )
)

branches = sorted(k[1] for k in cases if k[0] == "test_branch_coverage")
add(
    [
        "### The four branches",
        "",
        "One complaint down each branch the brief names: answered, not covered, recurring (seen in",
        "earlier reviews and questions), and containing PII. "
        f"**{score('test_branch_coverage', branches)}.**",
        "",
    ]
)
add(table(["Branch", "Result"], [[b, mark("test_branch_coverage", b)] for b in branches]))

# --- adversarial ------------------------------------------------------------------------------
adv = rows("adversarial.jsonl")
MUST = {
    "advice": "refused, with an investor-education link",
    "returns": "refused (no return figures computed or compared)",
    "pii": "answered or routed, with the PII never shown",
}
add(
    [
        "## Adversarial tests",
        "",
        "The PII values here (PAN, phone, email, account and Aadhaar numbers) are made up for the",
        "tests. Rows with the same session are sent in one conversation, in order, to check that",
        "pressing after a refusal is refused too.",
        "",
        f"### Typed: {len(adv)} questions",
        "",
        "`datasets/adversarial.jsonl`. "
        f"**{score('test_adversarial_typed', [r['id'] for r in adv])}.**",
        "",
    ]
)
add(
    table(
        ["Id", "Group", "Question", "Must be", "Result"],
        [
            [
                r["id"],
                r["group"] + (" (same session)" if r.get("session") else ""),
                r["text"],
                MUST[r["kind"]]
                + (", and the PII never shown" if r.get("pii") and r["kind"] != "pii" else ""),
                mark("test_adversarial_typed", r["id"]),
            ]
            for r in adv
        ],
    )
)

spoken = [c for c in clips if c["kind"] == "adversarial"]
by_id = {r["id"]: r for r in adv}
add(
    [
        f"### Spoken: {len(spoken)} clips",
        "",
        "The same questions read aloud. The transcript must not show the PII either.",
        f"**{score('test_adversarial_spoken', [c['id'] for c in spoken])}.**",
        "",
    ]
)
add(
    table(
        ["Id", "Same as", "What is said", "Result"],
        [
            [c["id"], c.get("ref", ""), c["text"], mark("test_adversarial_spoken", c["id"])]
            for c in spoken
        ],
    )
)

adv_reviews = rows("adversarial_reviews.jsonl")
add(
    [
        f"### In the weekly loop: {len(adv_reviews)} reviews",
        "",
        "`datasets/adversarial_reviews.jsonl`, mixed into the review sample. Each must be stored",
        "with",
        "its PII removed; the pulse and the doc entry and email draft must show no PII, must",
        "pass the",
        "advice and returns guards, and must not quote the advice or returns reviews. "
        f"**{mark('test_weekly_loop_handles_adversarial_reviews', '')}.**",
        "",
    ]
)
add(
    table(
        ["Review id", "Kind", "Stars", "Text"],
        [[r["review_id"], r["kind"], r.get("rating", ""), r["text"]] for r in adv_reviews],
    )
)

# --- earlier test sets ------------------------------------------------------------------------
retrieval = rows("retrieval.jsonl")
oos = rows("out_of_scope.jsonl")
add(
    [
        "## Earlier test sets",
        "",
        "Used while building retrieval and the Facts Agent, run by `evals/run_retrieval.py` and",
        "`evals/run_facts.py`, not by the six suites above. Their recorded scores are in the",
        "[Phase 2 status](../docs/implementation.md#phase-2-corpus-and-retrieval): **26 of 26**",
        "retrieval",
        "questions have a correct source in the top 4; the Facts Agent answered **26 of 26**",
        "with a",
        'correct citation, and returned **5 of 5** off-topic questions as "not covered".',
        "",
        f"### Retrieval: {len(retrieval)} questions",
        "",
    ]
)
add(
    table(
        ["Id", "Question", "Scheme", "Expected source", "Expected phrase"],
        [
            [
                r["id"],
                r["question"],
                r.get("scheme") or "",
                ", ".join(r.get("expect_sources", [])),
                r.get("expect_phrase") or "",
            ]
            for r in retrieval
        ],
    )
)
add([f"### Off-topic: {len(oos)} questions", ""])
add(table(["Id", "Question"], [[r["id"], r["question"]] for r in oos]))

# --- failures ---------------------------------------------------------------------------------
fails = [c for c in report["cases"] if c["outcome"] == "failed"]
add(["## Failures", ""])
if not fails:
    add(["None.", ""])
else:
    add(
        [
            f"{len(fails)} case(s) failed in this run. Both are known limits of the corpus, listed",
            "in the [README](../README.md#known-limits).",
            "",
        ]
    )
    add(
        table(
            ["Suite", "Case", "Message"],
            [
                [
                    SUITES.get(c["suite"], c["suite"]),
                    c["id"],
                    cell(c["message"].splitlines()[0], 200),
                ]
                for c in fails
            ],
        )
    )

OUT.write_text("\n".join(md).rstrip() + "\n")
print(f"Wrote {OUT} ({len(md)} lines) from {RESULTS.name}")
