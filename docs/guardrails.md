# Guardrails: Detection Rules and Decisions

How the code-level safety layer (Phase 1) decides what counts as PII, advice and return claims, and every known false positive with the decision taken. Read this with [architecture.md](architecture.md) section 6.

The rules live in code and data files:

| What | Where |
|---|---|
| PII detectors | `backend/app/guard/detectors.py`, `backend/app/guard/names.py` |
| Spoken-number handling | `backend/app/guard/spoken.py` |
| Advice rules | `backend/app/policy/rules/advice.yaml` |
| Returns rules | `backend/app/policy/rules/returns.yaml` |
| Test cases | `backend/tests/guard/`, `backend/tests/policy/` |

---

## 1. Where each check runs

| Check | Ingress (customer and staff text) | Outgoing drafts | Database writes | Log lines |
|---|---|---|---|---|
| Structured PII (PAN, Aadhaar, phone, email, UPI, account, OTP, long numbers) | Redacted | Hard fail | Write refused | Redacted and counted |
| Person names | Redacted | Not checked | Not checked | Not checked |
| Spoken numbers and emails | Normalised, then checked | Normalised, then checked | Not normalised | Not normalised |
| Advice rules | Not applied | Hard fail | Not applied | Not applied |
| Returns rules | Not applied | Hard fail | Not applied | Not applied |

**Why names are checked only at ingress.** Corpus facts can legitimately name people, such as a scheme's fund manager. Blocking any draft containing a name would block correct answers. Customer names are removed when they enter the system, so they never reach a draft in the first place.

---

## 2. PII decisions

| Case | Decision | Reason |
|---|---|---|
| 12 digits in 4-4-4 groups with a failed Aadhaar checksum and no "Aadhaar" label | **Not redacted** | Random 12-digit numbers are common. The checksum or a label is required. |
| The same number with an "Aadhaar" or "UID" label nearby | Redacted as Aadhaar | A mistyped Aadhaar is still sensitive. |
| 11 to 18 digits written together with no label | **Redacted** as an id number | Fail closed. These are usually account, card or reference numbers. |
| 4-digit years such as 2024 near the word "OTP" | Not redacted | "In 2024 the OTP rules changed" is not an OTP. |
| "PIN code 400001" | Not redacted | Postal codes are not one-time PINs, and the brief doesn't list addresses. |
| AMFI scheme codes such as "scheme code 119551" | Not redacted | Public identifiers that may appear in corpus facts. |
| ISINs such as INF179K01BB8 | Not redacted | Public identifiers. |
| PAN-shaped uppercase text with an invalid 4th letter, such as ABCDE1234F | **Redacted** | People often type example PANs this way. No real word has this shape. |
| Lower-case or spaced PAN-shaped text such as "March 2024 a" | Not redacted unless the 4th letter is a valid PAN holder type or "PAN" is written nearby | Avoids redacting ordinary words followed by a year. |
| Spoken digits: "nine eight seven six…" | Converted to digits, then checked | Voice transcripts must be checked like typed text. |
| Short spoken numbers: "one or two funds" | Left as words | Only runs of 4 or more spoken digits are converted. |
| UPI ids such as name@okhdfcbank | Redacted | Not in the brief's list, but a direct payment identifier. Added as a cheap, safe extension. |
| Card numbers in 4-4-4-4 groups | Redacted as an id number | Fail closed. |

**Known limits**
- spaCy's small model misses some names, especially lower-case Indian names without an introduction like "my name is". The "my name is", "this is" and "name:" patterns catch the common cases.
- The database write guard covers ORM writes and ORM-enabled `insert()` and `update()` statements. Raw SQL run directly on a connection is not checked. All user text must be written through the ORM.
- SQLAlchemy is configured to hide bound parameters, so values never appear in database error messages.

---

## 3. Advice decisions

The advice rules block investment directives, recommendations, timing opinions and product judgements. Each rule has a scope. The weekly pulse is internal and may recommend **product** changes, so the generic "we recommend" rules don't apply to it. The investment-advice rules apply everywhere.

### False positives accepted

These phrasings are blocked even though some readers wouldn't call them investment advice. The decision is to accept the false positive, because a facts-only reply can always be rephrased, and the reviewer's error message names the rule so the rework attempt knows what to change.

| Blocked text | Rule | Decision and preferred phrasing |
|---|---|---|
| "You should hold the units for 3 years due to the lock-in." | directive_investment_action | Accept. Write "ELSS units are locked in for 3 years." |
| "We recommend you contact support." | first_person_recommendation | Accept. Write "Please contact support." |
| "It is better to keep your KYC updated." | advisable_to | Accept. Write "KYC must be up to date to transact." |
| "Online redemption is a good option if you need money quickly." | product_judgement | Accept. Write "Redemptions can be placed online." |
| A quoted document sentence containing "stay invested" | stay_invested | Accept. Paraphrase the fact without the phrase. |
| "You may want to consider the factsheet." | none (allowed) | Not blocked: "consider" is only blocked before an investment verb. |

### Allowed on purpose

| Text | Why it passes |
|---|---|
| "You should receive your statement by email." | "you should" is only blocked before an investment verb, and process phrases are on the allow list. |
| "You can redeem units online after the lock-in." | Describes what is possible, not what to do. "can" and "must" are not treated as directives, because lock-in and process facts use them. |
| "You need to complete KYC before investing." | Process requirement. |
| "Please consult a SEBI-registered investment adviser." | The standard refusal pointer, on the allow list. |
| "Choose the Growth option or IDCW option at the time of investing." | Describes a form choice, not a recommendation. |

### Known misses

Keyword rules can't catch every advice phrasing. Examples that pass the rules: "Many investors prefer the direct plan" and "Long holding periods have historically suited equity funds". Phase 4 adds a language-model judge in the reviewer. It can only add failures, never clear a rule failure, so it backs up these rules without weakening them.

---

## 4. Returns decisions

Two checks: a percentage near a return word fails unless a fee or tax word is closer to it, and claim patterns fail even without a number.

| Text | Result | Reason |
|---|---|---|
| "The expense ratio of the Direct Plan is 0.67%." | Pass | Fee figure. |
| "Stamp duty of 0.005% applies on purchases." | Pass | Fee figure. |
| "Long-term capital gains above ₹1.25 lakh are taxed at 12.5%." | Pass | "capital gains" is masked as a tax term. |
| "HDFC ELSS Tax Saver - Direct Plan - Growth" | Pass | Plan names with "Growth" are masked. |
| "The fund's past performance is available in the factsheet." | Pass | Mentions performance with no figure or claim. This is the refusal pointer for return questions. |
| "TER is 0.67% and the fund has given 15% returns." | Fail | The second figure is nearer "returns" than any fee word. |
| A pulse quoting a review: "my returns were 12% last year" | Fail | The rule applies to the internal pulse too. The weekly loop must pick a different quote. |
| "The NAV is expected to grow." | Fail | Future performance claim without a number. |
| "The scheme outperformed its benchmark." | Fail | Performance claim. Naming the benchmark alone is allowed. |

**Template rule.** Refusal and not-covered templates must pass every rule. For example, "I can't share expected returns" would fail on "expected returns". The approved wording is "I can't share return or performance figures. The latest factsheet has the scheme's official performance data."

---

## 5. Changing the rules

1. Add or edit a rule in the YAML file, with an id, scope and note.
2. Add at least one blocked case and one allowed case to the tests.
3. If the change accepts a new false positive, add it to the tables above with the decision.
4. Run `uv run pytest tests/guard tests/policy` from `backend/`.
