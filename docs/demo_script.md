# Demo Script: Connecting the Two Loops

This demo shows the core of the project: the response loop and the weekly loop feeding each other (architecture section 9). Each step is given as a `curl` call against the local API from `backend/`. Every step can also be done in the frontend (`npm run dev` in `frontend/`): approvals on the Approvals page, questions in Chat, reuse counts on Explainers, and runs on Weekly pulse. The results quoted below are from the Phase 6 exit check on 18 November 2026.

## Before you start

- **Server.** Run `uv run uvicorn app.main:app` from `backend/`, with `APP_ENV=local`.
- **Trigger token.** `WEEKLY_TRIGGER_TOKEN` is set in `.env`. Export it as `T` for the weekly calls.
- **A weekly run with a fee explainer.** One that is awaiting approval, with its `promote_canonical` action still pending. The November sample data gives "Expense ratio and Direct vs Regular plans".

## Part 1: weekly loop → response loop

### 1. Show the fee explainer the weekly loop wrote

```bash
curl -s localhost:8000/actions/pending
```

Find the `promote_canonical` action. Its payload is the explainer:
- 4 bullets;
- 2 official sources (a SEBI FAQ and the HDFC TER sheet);
- a `Last checked` date.

**Point to make:** approving the weekly doc append or the email draft does *not* change how customers are answered. Promotion is its own approval.

### 2. Approve the promotion

```bash
curl -s localhost:8000/actions/<promote id>          # copy payload_hash
curl -s -X POST localhost:8000/actions/<promote id>/approve \
  -H 'content-type: application/json' -d '{"payload_hash": "<hash>"}'
curl -s localhost:8000/explainers/canonical
```

The explainer is now active, with a reuse count of 0. Each bullet is linked to the corpus chunk it came from, so replies can cite it.

### 3. Ask a related question

```bash
curl -s -X POST localhost:8000/chat/message -H 'content-type: application/json' \
  -d '{"text": "Why is my regular plan charging more than the direct plan?"}'
```

**Expected result:**
- **Classification.** Intent `fee_canonical`, fee type `expense_ratio`.
- **Reply.** The explainer's own bullets, word for word: "The Regular Plan Total TER for HDFC Liquid Fund is 0.3%. The Direct Plan Total TER for HDFC Liquid Fund is 0.2%."
- **Source.** The HDFC TER sheet, with its Last checked date.
- **Draft.** `source_type` is `canonical_explainer`, and `canonical_explainer_id` points to the explainer.
- **Routing.** "Answered with the approved canonical explainer".
- **Behind the scenes.** No model wrote the reply, and the Facts Agent didn't run.

### 4. Show what is *not* answered from the explainer

```bash
curl -s -X POST localhost:8000/chat/message -H 'content-type: application/json' \
  -d '{"text": "What is the exit load of HDFC Liquid Fund?"}'
```

This question names a scheme and a different fee type, so it is answered from the scheme's own documents (`source_type = corpus`).

## Part 2: response loop → weekly loop

### 5. Ask 4 more questions like the one in step 3

```bash
for q in "Why does my regular plan charge more than the direct plan?" \
         "Why is the regular plan charging more than direct plan?" \
         "why is my regular plan charging me more than the direct plan" \
         "Why is my regular plan charging more than my direct plan?"; do
  curl -s -X POST localhost:8000/chat/message -H 'content-type: application/json' \
    -d "{\"text\": \"$q\"}" > /dev/null
done
curl -s localhost:8000/explainers/canonical    # reuse_count is now 5
```

All 5 questions join one rolling question cluster. On the fifth, the cluster has 5 items within 7 days, and it is marked `surfaced`.

### 6. Run the weekly loop

```bash
curl -s -X POST localhost:8000/weekly/run -H "x-trigger-token: $T" \
  -H 'content-type: application/json' -d '{}'
curl -s localhost:8000/weekly/<run id> -H "x-trigger-token: $T"   # until awaiting_approval
```

**Expected result:**
- **Support-queue theme.** `themes` has an entry with `from_support_queue: true` ("Regular vs Direct plan fees"). It carries a count of 5, a window, and the 5 queue item ids.
- **Review themes.** They are unchanged: the top 3 still come from reviews.
- **Pulse.** It contains "From the support queue (similar customer questions): Regular vs Direct plan fees (5 questions, 18 Nov to 18 Nov)".
- **Pulse check.** It fails the run if a surfaced cluster is missing.
- **Doc entry.** The pending doc append's payload lists the cluster under `support_queue`, with its queue item ids.

## Part 3 (optional): replacing an explainer

**7.** Approve the new run's `promote_canonical`, which is a newer explainer for the same fee type. Then:

```bash
curl -s "localhost:8000/explainers/canonical?include_archived=true"
```

- **Old and new.** The older explainer shows `active: false` with an `archived_at` date.
- **Next reply.** The next related question uses the new explainer.
- **Reuse counts.** Each explainer keeps its own count.

## Where to look in the code

| Step | Code |
|---|---|
| Surfacing (5 in 7 days) | `backend/app/connector/clusters.py` |
| Clusters into the weekly run, and the pulse check | `backend/app/weekly/pipeline.py` (`queue` stage), `backend/app/weekly/generate.py` |
| Promotion and bullet-to-chunk links | `backend/app/gate/service.py`, `backend/app/connector/canonical.py` |
| Canonical reply, routing and reuse count | `backend/app/connector/canonical.py`, `backend/app/orchestrator/desk.py` |
| Tests | `backend/tests/connector/` |
