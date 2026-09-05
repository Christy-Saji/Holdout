# What broke

Incidents recorded as they happened, not reconstructed afterwards. Each one names the
symptom, what it would have cost if it had shipped, and how it was caught.

---

## 1. Three RBI boundary tests passed for the wrong reason

**Phase 4. Caught before the rule shipped, by its own mirror-image test.**

`RBIPreDebitNotice` compares timestamps rather than hour indices, so that the 24-hour
pre-debit window can be probed at minute resolution instead of being rounded onto the
simulator's hourly grid. The test helper that builds a policy context defaults its
clock to `state.hour`, which is 0 unless a test says otherwise, and four of the
pre-debit tests forgot to say otherwise.

The consequence was that the rule was being evaluated as if the debit happened at the
moment of failure, so every computed lead time was **negative** — `-23.98h`,
`-24.0h`, `-2.0h`. Every one of those denials was correct by accident.

The failure that exposed it was not the denial test. It was the three *allow* tests
sitting next to it:

| Test | Expected | Got |
|---|---|---|
| notice 23h59m prior | denied | denied ✓ (for the wrong reason) |
| notice 24h01m prior | allowed | **denied** ✗ |
| notice exactly 24h prior | allowed | **denied** ✗ |
| complete, timely notice | allowed | **denied** ✗ |

**What it would have cost.** Had the boundary been tested only from the denying side —
which is the natural way to write a compliance test, because the interesting case is
the violation — all four tests would have passed, the rule would have looked verified,
and its actual behaviour at the 24-hour boundary would never have been exercised once.
A regulation test that cannot distinguish "correct" from "always denies" is not
evidence of compliance; it is evidence that something denies.

**The fix.** The four tests now pass `at_hour=48` explicitly, so the debit sits two
days after the failure and the notice's lead time is a real positive number.

**The lesson, kept.** Every threshold in the RBI suite is now tested from **both**
sides — 23h59m denied *and* 24h01m allowed, ₹15,001 denied *and* ₹14,999 allowed,
₹1,00,001 denied *and* ₹99,999 allowed. A one-sided boundary test is indistinguishable
from a rule that is stuck.

---

## 2. A development run silently truncated the committed cohort from 500 cases to 20

**Phase 5. Shipped into the working tree and caught by `git status` minutes later.**

`eval` regenerates the cohort as a side effect, so that a reviewer with a clean clone
runs one command and gets byte-identical input. That is a good property. The
implementation of it wrote `data/cohort_seed42.jsonl` unconditionally, at whatever
`--n` was passed.

So a single development run —

```
python -m recovery.cli eval --seed 42 --n 20 --arms control,baseline,agent
```

— replaced the committed **500-case** reproduction artifact with a **20-case** one. The
run itself was perfectly correct; the cohort is a pure function of `(seed, n)` and 20
cases is a valid cohort. The damage was entirely to the file on disk.

**What it would have cost.** This is the worst shape a bug can have in a submission
whose entire argument is *clone this and get the same number*.

The 20-case cohort is a strict **prefix** of the 500-case one — same generator, same
seed, `case-00000` through `case-00019`, byte-identical rows. So the truncated file is
not corrupt, not malformed, and not obviously wrong in any way a reviewer or a linter
could see. Every line in it is a real, published case. Only the line count differs.

Committed, `python -m recovery.cli repro` would have **passed**. It would have
regenerated 20 cases, compared them against the 20 cases in the file, found them
identical, and reported a clean reproduction — of a cohort that is not the one the
README quotes a number for. The headline figure and the artifact that is supposed to
prove it would have silently drifted apart, with the gate that exists to catch exactly
that reporting success.

**The fix.** `eval` now writes the cohort artifact only when there is none — a clean
clone still gets a self-contained run — and refuses to overwrite an existing one whose
case count differs, printing what it declined to do rather than doing it quietly. The
in-memory cohort for the run is unaffected, so `--n 20` still runs 20 cases.
Replacing the artifact is now the sole job of `cohort --seed 42 --n 500`.

**The lesson, kept.** A command that *reports* a result should not also *overwrite the
artifact the result is checked against.* Two regression tests hold the line: one asserts
that `--n 20` against an existing 500-case file leaves the bytes untouched and says so
on stdout, and one asserts a clean clone still gets the file written.

---

## 3. The agent arm was built, tested, and never run

**Phase 5. Not a bug — a constraint, and a decision about what to do with it.**

This entry is different in kind from the two above. Nothing malfunctioned. It is here
because the submission prompt asks how you got out, and because a reader who reaches
the results table and finds two arms where the architecture describes three deserves
the reasoning rather than a silence.

`recovery/agent/` implements the agent arm: six `@beta_tool` functions, three of which
call the policy engine internally and return a refusal the model must react to, driven
by `client.beta.messages.tool_runner`, with cassette record/replay so that published
numbers reproduce offline. `tests/test_agent_gating.py` covers it with 23 tests that
pass with **no API key present**.

**It has never made a single live API request.** `ANTHROPIC_API_KEY` was listed as a
hard blocker from day 3 in the project's own risk table and it never arrived. Without
a recording pass there are no cassettes, so replay raises on the first case and the
arm cannot run at all.

**What it cost.** The headline number is control-versus-baseline rather than
control-versus-agent — a rules-based incumbent measured against a holdout, not an AI
system measured against one. For a track named *AI Revenue Recovery*, that is the
expensive part, and it is not recoverable by rewriting the README.

**The three ways out that were considered and rejected**, with the reasoning, because
the rejections are the actual content here:

| Option | Cost | Why not |
|---|---|---|
| Port the agent layer to a cheaper or free provider (Groq, Gemini, a local model) | ~$2–6 to record, plus roughly a day of work | Every one of them is OpenAI-shaped. `tool_runner` and `@beta_tool` would both have to be rewritten, `cache_control` would be lost, and the architecture's headline decision — *tool runner because Anthropic's guidance puts approval gating inside the tool function* — would be defending a choice no longer made. With one day left and three of four judged artifacts unwritten, this was a day spent to save roughly $15 |
| Train a model instead of calling one | Free | The cohort is synthetic and self-generated, so any model fitted to it recovers this project's own simulator parameters. `scoring.py` already documents why its curve deliberately differs from the simulator's: *"assuming we had recovered it exactly would be the tell of a model fitted to its own data."* Doing it anyway would contradict a design decision the repository argues for in writing |
| Drive the API from a Claude Pro subscription | Free | Outside what a consumer subscription licenses, and it would break the reproducibility claim, which depends on cassettes recorded from a real API. For a submission aimed at a role owning a money-moving service, this is the wrong thing to be caught doing |

**What was preserved instead.** The measurement spine never depended on the agent arm.
Control versus baseline is a complete result — ₹82,931 incremental, 95% interval
[₹61,156, ₹105,285], sign surviving a ±50% sweep of the parameter the whole design
rests on, every artifact byte-reproducible offline by `python -m recovery.cli repro`.
The agent layer stays in the repository, tested, because the tests are the evidence
that survives the missing number: a denied tool never reaches the transport, and that
is demonstrable without a credential.

**The lesson, kept.** A blocker that is known on day 0 and still unresolved on the last
day was never really a blocker on the schedule — it was a blocker on the plan. The
project's own risk table said "get it now" and dated the hard stop correctly; what was
missing was a decision point that forced the fallback early enough to be designed
rather than absorbed. The architecture happened to survive it, because the mock
transport was the default and the harness treats arms uniformly. That was luck built
on an earlier good decision, not foresight about this one.

---

## The five adversarial passes

**Phase 7.** Five deliberate attacks, all offline against the mock transport, in
`tests/test_adversarial.py`. Three held. Two turned up something, and both are written
up below with the number attached.

| # | Attack | Result |
|---|---|---|
| 1 | The same payment-success webhook delivered twice | **Held.** One recovery, ₹0 double-counted, one follow-up fired |
| 2 | Customer pays at hour 100 with a retry queued for 110 and an SMS for 120 | **Held.** Both refused by `TerminalState`, both refusals in the ledger, ₹0 spent |
| 3 | A stolen card mislabelled `insufficient_funds` | **Cost ₹70.00 before the cap stopped it** — incident 4 |
| 4 | An agent looping against a `BudgetCeiling` denial | **₹0 spent, but the turn budget is shared** — incident 5 |
| 5 | The 24h RBI notice boundary under clock skew | **Held.** Identical verdict across 250 evaluations and a process restart |

Pass 5 grew a test the other four did not need. Asserting that *one* rule ignores the
wall clock proves nothing about the eleven beside it, so
`test_no_module_in_recovery_reads_a_wall_clock` parses every module under `recovery/`
and fails on any call to `.now()`, `.utcnow()`, `.today()` or `time.time()`. Written
first as a substring search, it reported five violations — all five of them docstrings
saying *"never `datetime.now()`"*. The AST version is the one that means something.

---

## 4. A mislabelled hard decline cost ₹70.00 on a payment that could never succeed

**Phase 7, adversarial pass 3. Not a defect — a measurement of one, which is what the
pass was for.**

A case arrives labelled `insufficient_funds`. The truth is a dead instrument: nothing
can ever collect. The label is wrong at the input, so `HardDeclineNoRetry` cannot fire —
it reads the taxonomy, and the taxonomy says soft. `AttemptCap` reads the scorer, and
the scorer reads the same wrong label. **The system cannot detect this and it is not
supposed to be able to.** The question the pass answers is what it costs before the
economics stop it on their own.

Driven by a maximally greedy arm — retry every hour the gate allows — against a ₹4,000
case:

| | |
|---|---|
| Attempts executed | **7**, at hours 24, 48, 72, 96, 120, 144, 168 — one a day for a week |
| Gateway fees | **₹21.00** (7 × ₹3.00), the invoiced number |
| Modelled approval-goodwill damage | **₹49.00** (7 × `APPROVAL_DEGRADATION_COST_PAISE`) |
| **Total** | **₹70.00** on a ₹4,000 payment with no possible outcome |
| Policy evaluations | 336 — one per hour, every rule, every one in the ledger |
| Stopped at | Attempt 8, hour 169: expected recovery 324p < marginal cost 999p |

**The number that is not on the invoice is the larger one.** ₹21 in fees is
unremarkable. Seven consecutive declines that the issuer sees against the same
instrument is the part that degrades the merchant's approval rate on transactions that
would otherwise have succeeded, and that cost is carried by every *good* payment
afterwards. It is in the model at 700 paise per failed attempt because leaving it out
makes a ₹3 retry so cheap that `AttemptCap` barely binds inside a 336-hour window —
which is an honest result and a useless rule.

**What it means for the design.** The stopping condition works: it is `P(success) ×
amount < marginal cost`, evaluated per case, and on this case it bit at attempt 8
without any attempt-count constant existing anywhere in the rule. But it bit *seven
attempts in*, because it can only be as good as the label it is fed. The control that
would actually help here is not a better cap — it is a decline-reason feed trustworthy
enough that `HardDeclineNoRetry` gets the chance to fire, plus an observed-failure
signal that overrides the label after two or three attempts. Neither exists in this
project, and pretending the cap covers the gap would be the wrong lesson.

The contrast is asserted in the same file: the identical case *correctly* labelled
`stolen_card` costs **₹0.00**. `HardDeclineNoRetry` refuses before any money moves. The
whole ₹70 is attributable to the bad input, not to the policy.

Scaled naively: at 1% mislabelling across a 500-case cohort, five such cases cost ₹350
and 35 issuer-visible declines per run.

---

## 5. The agent can argue with the gate using the turns it needs for planning

**Phase 7, adversarial pass 4. A real finding, and smaller than it first looked.**

A ₹1.00 case: expected recovery is 33 paise, so the second 20-paise SMS breaches
`BudgetCeiling`. The pass then does what a badly-behaved model does — re-issues the
same denied action ten more times.

The money guarantee holds absolutely, and it holds structurally rather than by luck. A
denied action is never queued, never executed and costs nothing, however many times it
is re-issued: `_screen_and_queue` gates before it queues, and there is no path from a
tool to the transport. Ten stubborn re-issues produced **₹0.00 of spend, 0 executed
actions, and 11 refusals in the ledger** — every one of them auditable.

**What is not bounded is the turn budget.** `Session.denials` is incremented on every
refusal and then never read by anything that could stop the loop. `MAX_PLANNED_ACTIONS`
caps the plan at 8, but it counts *approved* actions only, so a model that never gets
one approved never approaches it. The only real bound is `MAX_ITERATIONS = 12` turns
per case — and that budget is shared with productive work. **A model that spends eleven
turns arguing with `BudgetCeiling` has one turn left to plan the case with**, and the
plan it produces is not a plan.

**Why it is being reported rather than fixed.** The fix is three lines — stop the
session after N consecutive denials and tell the model to close the case — but adding
it now would be tuning against a failure mode nobody has observed. **Whether Claude
actually loops is untested and cannot be tested here:** recording that behaviour needs
an API key, the cassettes do not exist, and incident 3 explains why. What the pass
establishes is the bound that holds *regardless* of what the model does, which is the
half that can be demonstrated without a credential. The half that cannot is stated as
unknown rather than asserted from a stub that would only be checking its own script.

---

## 6. Razorpay test-mode keys never arrived either

**Phase 7. A scope decision, recorded so that it is not a silence.**

`RAZORPAY_KEY_ID` and `RAZORPAY_KEY_SECRET` were never set, so
`recovery/transport/razorpay_test.py` **has never made a live call**. It is written
against the `razorpay` SDK's documented surface — Orders for the card and UPI retry
path, `payments/create/recurring` for the e-mandate debit the RBI rules actually
govern, Payment Links for the contact-to-pay path, and signature-verified webhooks —
and it is unexercised. The two tests that would hit the sandbox carry a `live` marker
and skip without credentials, so `pytest` stays green for a reviewer who has no keys.

Two things are asserted about it unconditionally, because they are the ones worth
having with no account at all: a missing credential raises an error naming the variable
that is unset, and **a `rzp_live_` key is refused outright**. Pointing a dunning system
at a live account through an environment-variable mistake is the worst accident
available in this repository, and the guard against it costs one string comparison.

**This one cost far less than incident 3, and for a reason worth naming.** The mock
transport was the default from phase 3 rather than a fallback added when the keys
failed to appear, so nothing downstream ever depended on the live path. The published
numbers come from the deterministic simulator **by design and not by accident**: a
network result is not reproducible, would differ between two runs a day apart, and
would break the clean-clone gate that `python -m recovery.cli repro` enforces. Had the
live transport been load-bearing, this would have been the second unrunnable arm rather
than a paragraph.

---

## 7. The published interval was computed at half the replicates the spec asked for

**Phase 6, found in phase 8 while auditing the exit criteria. It moved a published
number.**

The build plan §7 and the phase 6 prompt both specify the paired bootstrap at
**B = 2000**. `metrics.py` had `BOOTSTRAP_REPLICATES = 1000`, and the README quoted the
resulting interval as though it were the specified one. Nothing errored: a 1,000-replicate
percentile interval is a perfectly well-formed interval, just not the one the method
section described.

| | at B = 1000 | at B = 2000 |
|---|---|---|
| Incremental recovery | ₹82,931 | ₹82,931 — unchanged, it is not a bootstrap quantity |
| 95% interval | [₹61,156, ₹105,285] | **[₹61,719, ₹105,546]** |
| Interval width | ₹44,129 | ₹43,827 |

**The damage is small and that is the uncomfortable part.** The interval moved by about
₹560 at each end — well inside its own width, and the sign and the exclusion of zero were
never in doubt. Had the discrepancy been material it would have been caught by someone
checking the number; because it was immaterial, it survived every review until the
specification itself was re-read line by line. A methods section that does not match the
code is a defect at any magnitude, because the reader cannot tell which of the two the
number came from.

Two related gaps surfaced in the same audit, both from the same cause — a phase marked
complete against its narrative rather than against its exit criteria:

- **Time-to-cash and contact fatigue were never implemented**, while the `metrics.py`
  module docstring said they "live further down this module". Both are named deliverables
  in phase 6. Time-to-cash turned out to carry a real result — **3.70 days to cash in the
  treated arm against 7.04 in the holdout**, a 3.34-day improvement that no other metric
  in the project was reporting.
- **The paired-versus-unpaired interval-width test did not exist**, though the phase 6
  exit criteria name it explicitly as "the test that proves the CRN design is actually
  paying off". It now does, and on its fixture the unpaired interval is **1.6× wider** —
  so the pairing was working all along, but nothing in the suite would have noticed if it
  had stopped.

**What it changed.** `BOOTSTRAP_REPLICATES` is now asserted against the specification in
`tests/test_metrics.py` rather than merely set, so the constant cannot drift from the
methods section again without a test failing.

---

## 8. The readout claimed to need no network and fetched its fonts over one

**Phase 8. A presentation defect, and the only one here a judge would have seen first.**

`docs/index.html` is described in the README as "a single self-contained page with its
data inlined, no server and no network". It pulled Archivo and IBM Plex Mono from
`fonts.googleapis.com` at render time — three network requests on every open.

Opened the way a reviewer actually opens it — a local file, often with no connection —
every face fell back to the system UI font. The palette, the spacing, the dark mode and
the tabular figures were all intact underneath; the page simply rendered in Segoe UI and
looked like an unstyled document. **The design was never the problem, and no amount of
looking at the CSS would have found it**, because the CSS was correct and the failure was
in a resource that silently did not arrive.

The fix is to fetch the faces once (`scripts/fetch_fonts.py`), commit them base64-inlined
as `scripts/fonts/fonts.css`, and have the builder read them from disk: **10 faces,
351,534 bytes**, latin and latin-ext — the extended subset is not optional, because the
rupee sign U+20B9 lives in it and is on almost every line of the page.

**What it changed.** `tests/test_viewer.py` now asserts that the generated page
references no external host at all, so the claim in the README is enforced by a test
rather than by intent. The one URL it is allowed to contain is the SVG namespace, which
is an identifier and not a fetch.

