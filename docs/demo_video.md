# Demo video script (under 3 minutes)

The brief asks for a video only if hosting isn't possible, but it is a useful fallback. It must
show:
- the weekly loop running end to end;
- one question going through the response loop;
- one reviewer rejection;
- the approval gate;
- one evaluation running;
- one question asked by voice.

This script also shows the canonical explainer being reused, so both loop directions appear.

Record the hosted app, or the local one if it isn't hosted yet.

## Before recording

- **Wake the API.** Open the app and sign in, so it doesn't sleep on camera.
- **Run the weekly loop shortly before recording.** It takes about 6.5 minutes on Render's free
  tier. Leave its three actions pending. On camera you trigger a new run and then switch to the
  finished one.
- **Prepare the voice question.** Have the microphone allowed and a quiet room.
- **Prepare the eval.** In a terminal, from `backend/`, have this command typed and ready:
  `uv run python ../evals/run_all.py -k "edit_correctness"`. It runs 7 cases in about 30
  seconds with the cache.
- **Keep Google open.** Have the Google Doc and Gmail drafts open in two other tabs.

## Script

| Time | Show | Say (roughly) |
|---|---|---|
| 0:00–0:15 | The Chat page, with the disclaimer banner | "A facts-only support desk for five HDFC Mutual Fund schemes. Two loops: support questions, and a weekly product pulse from app reviews. Nothing is sent without a person approving it." |
| 0:15–0:40 | **Weekly pulse** → **Run now**. Switch to the earlier finished run. Click a theme's count to show its review ids. Scroll to the pulse and the fee explainer. | "Every Monday a cron job runs this. Reviews are grouped into at most 5 themes, and each count lists its review ids and states its window. The data didn't support the exit-load hypothesis; the fee confusion it found is Direct vs Regular plans. The pulse is under 250 words." |
| 0:40–1:05 | **Approvals**: the doc entry card with its full payload. **Approve** it. Show the entry in the Google Doc. Back on Approvals, the email draft is still pending; approve it and show it in Gmail. | "Each write is its own approval. The card shows the exact payload, and the approval carries its hash. Approving the doc entry did not touch the email draft. The draft has no recipient and is never sent." |
| 1:05–1:20 | Approve **Promote to canonical explainer**. Go to Chat and ask "Why is my regular plan charging more than the direct plan?". Open **Explainers**: the reuse count goes up. | "Promoting the explainer is a separate decision. Related questions are now answered with its bullets, word for word." |
| 1:20–1:50 | Chat: press **Speak** and say "What is the exit load of HDFC Liquid Fund, and should I redeem now?". Show the transcript box, then **Send**. Two answer blocks appear: one cited, one refused. Press **Play reply**. | "Speech goes to Whisper; the audio is never stored, and the transcript is redacted before I see it. The message is split into two questions. The fact gets a cited answer with its Last checked date. The advice part is refused, with an investor-education link." |
| 1:50–2:15 | **Queue**: open the exit-load item. Show the timing bars (three agents starting together), each agent's output, and the reviewer's checks. | "Facts, Pattern and Compliance run in parallel. The orchestrator writes the draft, and the reviewer checks it against the hard rules." |
| 2:15–2:35 | In the same drawer, press **Edit** and add "You should definitely buy this fund now." to the reply, then save. The new version shows `advice_guard` failed and **Approve** is disabled. | "A reviewer rejection: the edit fails the advice guard by name, and the server refuses to approve a blocked draft." |
| 2:35–2:55 | Terminal: run the edit-correctness suite. Show the 7 passing cases, then the report in `evals/results/`. | "The evals run on the whole system. This suite snapshots every table before and after each decision; only the expected rows change. Six suites, 171 cases: zero PII leaks and zero unrefused advice questions." |
| 2:55–3:00 | The README | "The repo has the architecture, the evals and the known limits." |

## The recorded video

[docs/demo.mp4](demo.mp4) (2 min 54 s) follows this script on the live app, recorded with an
automated browser (Playwright driving Chrome). It differs in a few ways:
- **Captions, read aloud.** Each caption is spoken by a synthesised voice (macOS `say`, the
  Tara voice) as it appears. Where the speech runs longer than the caption was on screen, the
  last frame of that part is held until the voice finishes.
- **Waits for the live API are sped up**, 8 times, while a "sped up" tag is on screen.
  Everything else plays in real time.
- **The voice question is synthesised speech** (macOS `say`) fed in as the microphone. It goes
  through the real recorder, the upload and Whisper.
- **The Google Doc and Gmail aren't shown.** The browser isn't signed in to Google; each
  approval shows its card leaving the queue.
- **The eval is a recorded local run**, replayed at its real speed, with its wait before the
  first result sped up.
- **Run now comes last.** A run in progress slows the 0.1 CPU server, so approvals are shown
  first.

## Notes

- **If the Google tokens have expired**, the approved action shows "failed" and can be retried.
  Refresh the token before recording.
- **The reviewer rejection is deterministic**: the advice guard is a code rule, so the edited
  text is always blocked.
