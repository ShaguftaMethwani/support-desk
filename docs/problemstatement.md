# Graduation Project: The Support Desk

> A deployed, human-in-the-loop system that runs the support and product feedback loop for one Indian investment platform. It is facts-only, cited and compliance-checked.

---

## 1. Product & Scope

| Item | Value |
|---|---|
| **Platform** | **INDMoney**, the same product used in all milestones |
| **AMC (corpus scope)** | **HDFC Mutual Fund**, one of the largest AMCs distributed on INDMoney, with complete public scheme documents |
| **Schemes** | See the scheme table below |
| **Corpus** | 15–25 public pages from HDFC Mutual Fund, SEBI and AMFI. See the corpus plan below. |
| **Reviews data** | One public reviews CSV of INDMoney app reviews from the Google Play Store and Apple App Store, covering the last 8–12 weeks |

INDMoney is a distribution platform, not an AMC. The platform supplies the user reviews and the support context. The AMC supplies the official scheme facts.

### 1.1 Schemes in scope (Direct Plan, Growth option)

| # | Scheme | Category | Why it is in scope |
|---|---|---|---|
| 1 | **HDFC Large Cap Fund** | Large-cap equity | Baseline equity fund. Covers expense ratio, exit load, benchmark and riskometer questions. |
| 2 | **HDFC Flexi Cap Fund** | Flexi-cap equity | One of the most-held flexi-cap funds. Covers category and allocation questions. |
| 3 | **HDFC ELSS Tax Saver** | ELSS | Covers the 3-year lock-in, Section 80C questions and the "why can't I redeem" confusion. |
| 4 | **HDFC Mid Cap Fund** | Mid-cap equity | A higher riskometer level. Covers risk-label and benchmark questions. |
| 5 | **HDFC Liquid Fund** | Liquid debt | Covers the graded exit load for redemptions within 7 days, a common source of fee confusion. |

Scheme names change after SEBI recategorisation. Confirm each name against the current SID on the HDFC Mutual Fund website when building the corpus.

### 1.2 Corpus plan (target 20 pages)

| Source | Pages | Examples |
|---|---|---|
| HDFC Mutual Fund | 12–14 | Scheme pages, latest factsheet, KIM and SID for each scheme, total expense ratio disclosure, exit load and charges details, account statement and capital-gains statement guides, investor FAQ |
| SEBI | 3–4 | Mutual fund investor charter, riskometer circular, scheme categorisation circular, total expense ratio rules |
| AMFI | 3–4 | Investor education pages on SIP, ELSS, exit load and expense ratio, plus the riskometer explainer |

INDMoney help-centre pages may be used **only** for platform process questions, such as how to download a statement inside the app. They are never a source for scheme facts.

---

## 2. Objective

Build a **deployed web application** that runs the support and product loop for one Indian investment platform:

- **Reviews come in and become themes.**
- **User questions come in and become cited, compliance-checked reply drafts.**
- **A weekly pulse goes out to the product team.**
- **Nothing leaves without a human approving it.**

---

## 3. The Problem

Two teams are solving halves of the same problem, and neither can see the other half.

- **Support** answers the same fee question forty times a month. They write the answer from scratch each time and sometimes get it wrong.
- **Product** reads a review dashboard that says "pricing confusion is up". It doesn't say *which* fee, or what support has been telling users about it.

The missing link is data that neither team has in one place:

1. **What the official documents actually say.** This comes from the Milestone 1 corpus.
2. **What users are actually confused about.** This comes from the Milestone 2 review themes.
3. **How often it happens.** This comes from pattern counts across reviews and incoming questions.

That shared layer is what this project builds.

### Why a single model is not enough
A support agent answering *"Why was I charged an exit load?"* needs three things at once, and each comes from a different place:
- **The fact** is in a scheme document.
- **Whether it is a pattern** decides whether this is a reply or an escalation. Forty other users may have asked the same thing this month.
- **Whether the draft is safe** matters too. It must not give investment advice, quote a return figure, or repeat back a PAN the user pasted into the complaint.

A single model asked to do all three usually does the first and forgets the rest. It writes a fluent, uncited paragraph that ends with *"you may want to consider staying invested longer"*. That is advice, and it creates a compliance problem.

---

## 4. What You Bring Forward

| Milestone | Component reused |
|---|---|
| **M1: Facts-Only MF FAQ (RAG)** | Cited FAQ corpus over official AMC/SEBI/AMFI pages. Retrieval with one citation per answer. Facts-only refusal for advice questions. PII rejection. |
| **M2: Product Insights Copilot** | Review clustering into at most 5 themes. Weekly pulse. Fee explainer. Approval-gated MCP workflow that appends to a doc and creates an email draft. |
| **M3: Multi-Agent Support Desk** | Three parallel agents (Facts, Pattern, Compliance). Orchestrator. Reviewer with one rework round. Targeted refinement. Approval-gated MCP. |

**What's new in the graduation project:**
- Running all components as **one system on a schedule**
- **The interface** (queue, pulse, sources, approvals, canonical explainers)
- **The connection** between the weekly loop and the response loop
- **Scope enforcement in code**, not only in prompts
- **Evaluations** over the assembled whole
- **Deployment**

---

## 5. What You Build

A deployed web application with two loops, a connection between them, and enforcement across both.

### 5.1 The Weekly Loop (Product side)

Triggered on a schedule or manually. Once triggered, it completes with **no manual intervention** until it reaches the approval gate.

1. **Ingest** the reviews CSV covering the last 8–12 weeks.
2. **Cluster** reviews into **at most 5 themes**.
3. **Identify the top 3 themes.**
4. **Extract 3 real user quotes** that support those themes.
5. **Detect one recurring fee or charge confusion**, for example exit load charged unexpectedly, expense ratio or TER deductions, or stamp duty. The fee issue must come from the INDMoney reviews data. The hypothesis to test first is **exit load charged on early redemption**, because it applies to all five schemes in scope and has a graded structure on the liquid fund.
6. **Generate the Weekly Product Pulse**, at most **250 words**:
   - Summary of the top themes
   - Supporting user quotes
   - Key observation about what is going wrong or trending
   - **3 action ideas** for the product team
7. **Generate the Fee Explainer**, at most **6 bullets**:
   - Neutral, facts-only tone
   - **2 official source links**
   - `Last checked: <date>`
   - It must address the confusion found in step 5. It cannot be a made-up scenario.
8. **Approval gate.** Nothing is written until a human approves it.
9. **After approval:**
   - **Append to Notes/Doc** (for example Google Docs or Notion) with this record:
     ```json
     {
       "date": "...",
       "top_themes": [...],
       "weekly_pulse": "...",
       "identified_fee_issue": "...",
       "explanation_bullets": [...],
       "source_links": [...]
     }
     ```
   - **Create an Email Draft.** Never auto-send.
     - **Subject:** `Weekly Product Pulse + Customer Clarification — <date>`
     - **Body:** the weekly pulse, plus the fee explanation formatted as a reusable support snippet

**Example flow:** Reviews mention "exit load charged unexpectedly". The theme is pricing confusion. The fee issue is a misunderstanding of exit load. The output is the weekly pulse plus an exit load explainer. After approval, both are saved to the doc and an email draft is created.

### 5.2 The Response Loop (Support side)

A **queue** of incoming user questions or complaints. Questions arrive in three ways: typed into the chat, **spoken through the voice interface**, or pulled from the reviews CSV. All three enter the same queue and the same pipeline.

For each item:

**Three agents run in parallel.** No agent waits on or reads another agent's output.

| Agent | Responsibility | Output |
|---|---|---|
| **Facts Agent** | Retrieves the answer from the M1 corpus only. It never falls back on general knowledge. | The answer with **one citation link**, or **"not covered"** with the list of sources and queries it searched |
| **Pattern Agent** | Checks the question against the M2 themes and recent queue history | A label of **recurring**, **rising** or **one-off**, the **count** of reviews or questions behind it, and the **time window** used |
| **Compliance Agent** | Scans **both the incoming message and the outgoing draft** for advice language, performance or return claims, and PII | Flags, plus **redaction before anything is stored** of PAN, Aadhaar, account numbers, phone numbers, emails and OTPs |

**The orchestrator** merges the three outputs into two artefacts:
1. **Reply draft.** At most **3 sentences**, facts-only, with its citation link and `Last checked: <date>`.
2. **Internal routing note.** Which team the item goes to and why. Priority is set from the Pattern Agent's signal.

When the agents disagree, the orchestrator must say so. For example, Facts may have a clean answer while Pattern shows 40 users asked the same thing this week. In that case the orchestrator flags the escalation instead of quietly replying and closing the ticket.

**The reviewer** checks every draft before a human sees it. These are **hard fails**:
- A factual claim with no citation
- Anything that reads as advice
- Any PII that survived redaction

The reviewer sends a failed draft back to the orchestrator **once**, naming the specific failure. If the second attempt still fails, the UI shows the draft **with the flag on it** and **offers no send or approve action**.

**Refinement.** The human can push back, and **only the affected agents re-run**. Every refinement logs which agents fired. Refinement is capped at **5 rounds**.

| Human feedback | Agent that re-runs |
|---|---|
| "Shorter" | Orchestrator only |
| "They're asking about a different scheme" | Facts Agent |
| "Check if this is the fee issue from last week" | Pattern Agent |

**Human decision.** For each draft the human can **approve**, **edit** or **reject** it.

**Approval-gated MCP.** After approval, the system appends the record to the doc and creates an email draft. It never sends automatically.

### 5.3 The Connection Between the Loops (the core new work)

Neither milestone did this on its own. Build it and **show it working in the demo**.

- **Response loop to weekly loop.** When the response loop sees the **same question 5 or more times in a week**, it appears in the weekly pulse. It shows up as a theme or observation, with its count and window.
- **Weekly loop to response loop.** When the weekly loop identifies a fee confusion, the approved **fee explainer becomes the canonical answer**. The response loop **reuses** it instead of drafting a new answer. The reply should show that it came from a canonical explainer.

### 5.4 Scope Enforcement

These rules apply across **both loops** and are **enforced in code, not only in prompts**:
- Facts-only. No recommendations.
- No buy or sell language.
- No return or performance figures. If asked, link to the official factsheet.
- **No PII stored anywhere.** This covers the dataset, the interface, logs and agent state.

In practice this means deterministic checks wrapped around the language model, for example:
- Regex and validator-based PII detection and redaction **before** anything is persisted or logged
- Pattern and keyword guards for advice and returns language
- Validators that check citation presence and enforce sentence, word and bullet limits
- Approval checks at the code level before any write action runs

### 5.5 Conversational and Voice Interface

The rubric gives "Voice UX & intent handling" the largest weight. This project reads that as a **conversational interface that accepts both text and voice questions**.

The conversational interface is an **internal agent-assist console**. A support agent types or speaks the customer's question and gets reviewed drafts back. Customers receive a reply only through an approved action, so "nothing leaves without a human approving it" still holds.

**Voice pipeline**
1. **Speech-to-text.** The user speaks a question in the browser. The audio is transcribed to text.
2. **PII redaction on the transcript.** The Compliance Agent's deterministic redaction runs on the transcript **before** it enters the queue, the logs or agent state. Raw audio is **never stored**.
3. **Same pipeline as text.** The redacted transcript runs through the three agents, the orchestrator and the reviewer, exactly like a typed question.
4. **Spoken reply.** Once a draft passes the reviewer, the reply can be read aloud with text-to-speech. The citation link and last-checked date are always shown on screen, because a link cannot be spoken usefully.
5. **Transcript confirmation.** The UI shows the transcript before submission, so the user can fix misheard scheme names. "HDFC Mid Cap" misheard as "HDFC Big Cap" would retrieve the wrong document.

**Intent handling**

Every question, typed or spoken, is classified into one intent before the agents run. The intent decides the path.

| Intent | Example | Path |
|---|---|---|
| **Factual** | "What is the exit load on HDFC Liquid Fund?" | Full pipeline. Cited reply. |
| **Fee confusion with a canonical answer** | "Why was I charged when I withdrew?" | Reuses the approved fee explainer. |
| **Advice or opinion** | "Should I redeem my ELSS?" | Polite refusal with an AMFI or SEBI educational link. |
| **Performance or returns** | "What returns will I get?" | Refusal plus a link to the official factsheet. No figures. |
| **Out of scope** | "What's the NAV of a scheme outside the corpus?" | "Not covered", naming what was searched. |
| **Complaint or escalation** | "My redemption hasn't arrived for 10 days" | Routing note to the right team, priority set by the Pattern Agent. |
| **Unclear** | "Tell me about the fund" | One clarifying question, such as asking which scheme. |

**Conversation rules**
- Follow-up questions keep context within a session, for example "and what about the ELSS one?"
- Advice refusals hold across the whole session, even when the user rephrases or asks again later.
- The spoken reply follows the same 3-sentence cap as the text reply.

---

## 6. The Interface

| View | Must show |
|---|---|
| **Conversational view** | A chat interface with a **microphone button** for voice questions, an editable transcript, text replies with citations and an optional spoken reply. Shows a welcome line and 3 example questions. |
| **Queue view** | Incoming questions with their source (text, voice or CSV), detected intent, draft replies, citations, routing notes and any reviewer flags. Approve, edit and reject controls. Refinement controls. |
| **Weekly pulse view** | Themes, quotes, and the **counts behind them with their stated window**. Every theme can be traced to its review ids. |
| **Sources panel** | Where every factual claim came from, with its **last-checked date** |
| **Approval screen** | **Exactly what will be written and where**, shown before anything is written. One approval covers one specific action. |
| **Canonical explainers view** | The fee explainers the response loop is currently reusing |

Every UI surface also carries a disclaimer: **"Facts-only. No investment advice."**

---

## 7. Rules (Hard Constraints)

1. **Every factual claim carries one source link** from the M1 corpus. If there is no link, the claim doesn't ship.
2. **Public sources only.** Use AMC, SEBI and AMFI pages. No third-party blogs and no app back-end screenshots.
3. When the corpus doesn't cover a question, the system **says so and names what it searched**.
4. **Every theme traces to review ids**, and **every count states the window it covers**.
5. **No PII anywhere**, including the dataset, interface, logs, agent state and voice transcripts. Raw audio is never stored.
6. **Nothing is written or sent without a human approving that specific action.** Approving one action must not trigger another.
7. **Length caps:** reply drafts at most 3 sentences, the pulse at most 250 words, the explainer at most 6 bullets.
8. **No performance claims.** Don't compute or compare returns.
9. **Three agents run in parallel**, as shown in the architecture diagram and the demo.
10. **Reviewer rework is capped at 1 round. Refinement is capped at 5 rounds.**
11. Replies include `Last checked: <date>` and are facts-only. Advice and opinion questions get a polite refusal with a relevant educational link from AMFI or SEBI.

---

## 8. Evaluations

There must be **at least three checks**, run over the **assembled system** and not over isolated components.

### 8.1 Feasibility
- Every question in the queue produces either a draft or an explicit **"not covered"**.
- Every theme carries review ids and a stated window.
- The pulse stays within **250 words**, and the explainer within **6 bullets**.
- Once triggered, the weekly loop completes **without manual intervention**.

### 8.2 Correctness of Edits
Run each test, **diff the system state before and after**, and record the results:
- A human edit to one draft changes **only that draft**.
- Approving one action **does not trigger another**.
- Rejecting a draft leaves the **rest of the queue untouched**.

### 8.3 Grounding & Compliance
- **Claim tracing.** Sample **15 claims across both loops**. Each must trace to a source link or a review id.
- **Coverage gaps.** Feed in questions the corpus can't answer. The system must say so and name what it searched.
- **Adversarial checks.** Run these in **both loops**, rephrased and repeated within the same session:
  - Advice requests, such as "Should I redeem?", "Is this scheme good?" and "Which fund should I pick?"
  - Return-figure requests, such as "What returns can I expect?"
  - Messages containing a **PAN**, an **account number** and a **phone number**
  - The same requests **spoken through the voice interface**, so the transcript path is tested too
- Every one of these must be **refused or redacted**, and the logs must contain **nothing sensitive**. Grep the logs and stored state to prove it.

### 8.4 Voice and Intent Handling
- **Intent accuracy.** Build a labelled set of at least 30 questions across all seven intents. Report the accuracy of the intent classifier, plus a confusion table.
- **Voice parity.** Speak 10 questions from the labelled set. Confirm each produces the same intent and the same cited answer as the typed version.
- **Transcript redaction.** Speak a PAN and a phone number. Confirm they are redacted before the transcript reaches the queue or the logs.

### 8.5 Recommended additional checks (carried over from M3)
- Run **20 real complaints** from the reviews CSV. Confirm all three agents return structured output for every one, and every reply has a citation or says "not covered".
- **Branch coverage.** Feed in one complaint the corpus answers cleanly, one it can't answer, one matching a recurring theme, and one containing PII. Log which agents fired on each, and confirm the reviewer caught the PII.
- **Loop connection.** Seed 5 or more identical questions in a week and confirm they appear in the next pulse. Approve a fee explainer and confirm the next related question reuses it.

---

## 9. Deliverables

1. **GitHub repo** with a README that covers:
   - Architecture, including a diagram showing the three parallel agents, the orchestrator, the reviewer, both loops and the MCP gate
   - Setup and run instructions
   - The source list of 15–25 URLs from the M1 corpus
   - **Where the approval gate sits**
   - **How the two loops connect**
   - **Which fee issue came out of the reviews**
   - How to run the evals
   - Known limits
2. **Artefacts in the repo:**
   - Reviews CSV sample, with PII removed
   - 5 sample questions with their drafts
   - The appended doc entry
   - The email draft as a screenshot or text
   - Evaluation results
3. **Working prototype link**, either a hosted app or a notebook.
4. **Demo video under 3 minutes**, *only if hosting isn't possible*. It must show:
   - The weekly loop running end to end
   - One question going through the response loop
   - One reviewer rejection
   - The approval gate
   - One evaluation running
   - One question asked by voice

---

## 10. Evaluation Rubric

| Criterion | Weight |
|---|---|
| Voice UX & intent handling | 25% |
| MCP usage & system design | 20% |
| AI evals & iteration depth | 20% |
| Grounding & RAG quality | 15% |
| Workflow automation | 10% |
| Deployment & code quality | 10% |

> **Interpretation:** "Voice UX" is read as a conversational interface that answers both typed and spoken questions, with explicit intent handling. See section 5.5.

### What's being graded
- Can you design a system where **three data sources answer one question**?
- Do you know **where a language model stops being reliable**, and does your system **say so out loud**?
- Can you build a **gate that a compliance team would trust**?
- Can you **explain why you drew the lines where you did**?

---

## Appendix: Glossary

| Term | Meaning |
|---|---|
| **AMC** | Asset Management Company. It manages mutual fund schemes. HDFC Mutual Fund is the AMC in scope here. |
| **STT / TTS** | Speech-to-text / text-to-speech, used by the voice interface |
| **AMFI** | Association of Mutual Funds in India, the industry body. Publishes investor education material. |
| **SEBI** | Securities and Exchange Board of India, the regulator |
| **MF** | Mutual Fund |
| **ELSS** | Equity Linked Savings Scheme. Offers a tax benefit under Section 80C and has a 3-year lock-in. |
| **SIP** | Systematic Investment Plan |
| **KIM / SID** | Key Information Memorandum / Scheme Information Document |
| **TER** | Total Expense Ratio |
| **RAG** | Retrieval-Augmented Generation |
| **MCP** | Model Context Protocol. Used here for tool actions such as doc append and email draft, behind an approval gate. |
| **PII** | Personally Identifiable Information, such as PAN, Aadhaar, account numbers, phone numbers, emails and OTPs |
| **PAN** | Permanent Account Number, a 10-character tax ID |
| **OTP** | One-Time Password |
