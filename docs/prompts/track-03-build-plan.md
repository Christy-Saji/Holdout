# Track 03 — AI Revenue Recovery: build plan

> Working plan for the Razorpay AI Buildathon submission.
> Written 29 August 2026 · Submit 4 September · Deadline 5 September
> Strategy source: [research brief](https://claude.ai/code/artifact/59a24105-c673-4ee4-a148-d07c8a57657e)

---

## 1. Context

### What this competition actually is

The Razorpay AI Buildathon is not a prize hackathon. There is no prize pool, no
leaderboard, no judging gala. It is a hiring filter for a **₹75,000/month AI
Builder internship** in Bengaluru, 6 or 12 months, starting September, open to
the 2027–2029 graduating batches. You pick one of five tracks, build alone, and
submit four artifacts:

1. A public repository
2. A five-minute pitch video
3. An architecture write-up
4. A written answer to *"What broke and how you got out"*

Strong submissions go straight to a panel. No resume screen, no aptitude test,
no group discussion.

That changes the optimisation target. We are not trying to impress a judge for
ninety seconds. We are trying to make a Razorpay engineer read the repo and
think *this person could own a money-moving service on my team in September.*

### Why Track 03

The published bar for Track 03:

> "Don't just identify the problem. Show **measured money recovered across a
> batch**, with **compliant escalation**, **stopping rules**, and an **audit
> trail**."

Three reasons this is the right track:

- **It is the only track whose deliverable is a number in rupees.** That is the
  most persuasive artifact you can put in front of a payments company in five
  minutes.
- **Eight days is enough to produce verifiable evidence rather than a demo.**
  The other tracks either need labels we don't have (02), are hard to make
  legible on video (04), or compete head-on with shipped Razorpay products (01).
- **"Compliant escalation" and "stopping rules" are regulatory words.** Whoever
  wrote that bar has been burned in production. That is a bar we can clear
  deliberately rather than accidentally.

### The finding that shapes everything

Razorpay's Agent Studio (launched March 2026 on the Claude Agent SDK) already
ships an Abandoned Cart agent, a Dispute Responder, a **Subscription Recovery
agent**, and a Cashflow Forecaster. Those map one-to-one onto tracks 01–04.

**The obvious project in every track is a worse copy of a live Razorpay product
whose code the reviewer has read.** We cannot out-feature them in eight days.

So we do not compete on features. We compete at the **measurement layer**, which
is where their own published bar points and where a solo student can actually
be rigorous.

### The core bet

Nearly every submission will report a **gross** number: *"we recovered ₹4.2
lakh."* That figure is close to meaningless, because a meaningful share of those
payments would have recovered on their own — customers retry, cards get topped
up, temporary issuer blocks clear. Reporting gross recovery as if the agent
caused it is precisely the "cherry-picked match proves nothing" failure Razorpay
pre-emptively calls out in Track 04's bar.

**We report incremental recovery against a holdout control arm, and cost per
incremental rupee.** This is standard practice on any payments growth or data
science team, so it reads instantly as someone who already thinks the way they
do. Almost no student will do it.

### Starting state

| Fact | Value |
|---|---|
| Working directory | `C:\Users\chris\college\programming\project\razorpay` |
| Git | Initialised (`main`), no commits yet |
| Python | 3.11.9 |
| Node | 22.17.1 (unused — Python build) |
| `make` | **Not installed** — see §8 |
| `anthropic` package | Not installed |
| `razorpay` package | Not installed |
| `ANTHROPIC_API_KEY` | Not set — **hard blocker from day 3** |
| Razorpay test keys | Not obtained — needed day 5 only |

The brief's day 1 (28 August) has already passed, so its days 1 and 2 fold into
today. Seven build days remain.

---

## 2. Two deliberate departures from the brief

### 2.1 Anthropic SDK tool runner, not the Claude Agent SDK

The brief recommends building on the Claude Agent SDK on the grounds that Agent
Studio is built on it, so the architecture conversation lands in Razorpay's
vocabulary. That reasoning is sound but the conclusion is wrong.

The **Claude Agent SDK** (`claude-agent-sdk`) is Claude Code packaged as a
library: built-in Read/Write/Edit/Bash/Glob/Grep tools, a filesystem harness,
context management for coding work. It is a batteries-included *coding* agent.
Using it to decide whether to send an SMS at 14:00 means spinning up a coding
harness to make a scheduling decision, and it invites an obvious and damaging
panel question we would have no good answer to.

The right surface is the **tool runner in the regular Anthropic SDK**:
`client.beta.messages.tool_runner` with `@beta_tool`-decorated functions. It
drives the agentic loop over *tools we define*, with no built-in tools and no
sandbox.

Critically, Anthropic's own documentation states that human-in-the-loop
approval does **not** require a manual loop — you *gate inside the tool
function* and return a refusal result the model must react to. That is exactly
the seam this project needs:

- Every money or contact action is a tool.
- Every tool calls `PolicyEngine.evaluate()` before doing anything.
- A denial returns `{allowed: false, rule_id, reason}` to the model, which must
  then choose a different action.
- The denial is written to the ledger — **the blocked-action log builds itself.**

We keep the Razorpay-vocabulary benefit anyway by naming the choice explicitly
in `ARCHITECTURE.md` and explaining the reasoning. Stating why you did *not*
use the obvious thing reads as judgement. Silently using the wrong thing reads
as not knowing the difference.

### 2.2 Razorpay test-mode integration moves from day 4 to day 5

Credentials are deferred, so every arm runs against a deterministic mock
transport by default. This is risk management, not a downgrade — the brief
itself says to keep a mock transport so the harness never depends on the
network. Consequences:

- The evaluation harness never blocks on sandbox onboarding.
- If keys never arrive, the submission still stands on its own.
- The live integration becomes a *demonstration* of real API wiring rather than
  a load-bearing dependency of the results.

---

## 3. The methodological core

This section is the intellectual centre of the submission. Everything else is
engineering.

### 3.1 Latent ground truth

Each synthetic payment carries hidden state, drawn at cohort-generation time
from a seeded RNG and **never visible** to the agent, the scorer, or the policy
engine:

| Latent | Meaning |
|---|---|
| `p_self_heal` | Probability the payment recovers inside the window with no intervention at all |
| `retry_success_base` | Probability a well-timed first retry succeeds |
| `intent_to_pay` | Per-customer willingness multiplier |
| `responsiveness` | Per-customer contact-effectiveness multiplier |
| `channel_affinity` | Per-customer, per-channel multiplier (SMS / WhatsApp / email) |

Because the simulator owns these and the arms cannot see them, the counterfactual
is exact rather than estimated. We are not inferring what would have happened —
we know, because we specified it.

### 3.2 Common random numbers (the variance-reduction trick)

All three arms run against the **same cohort** with the **same latents**, and
every stochastic draw is derived deterministically from the event's identity
rather than from a sequential RNG stream:

```
u(seed, case_id, event_kind, sequence) =
    int.from_bytes(blake2b(f"{seed}|{case_id}|{event_kind}|{sequence}").digest()[:8]) / 2**64
```

This is **common random numbers**, a standard variance-reduction technique. Its
effect here is that the hour at which case #237 would self-heal is *identical*
in the control arm and the agent arm. The difference between arms is therefore
attributable purely to the intervention, not to sampling noise — which
dramatically tightens the confidence interval on incremental lift for the same
cohort size.

It also means arms can be run in any order, in parallel, or one at a time
across separate processes, and the comparison stays valid.

### 3.3 The outcome model

A discrete hourly simulation over a **14-day (336-hour) window** per case.

**Self-heal** is modelled as a per-hour hazard so it composes correctly with
interventions:

```
h_self = 1 − (1 − p_self_heal) ** (1 / W)        # W = 336
```

At each untouched hour, draw `u(seed, case_id, "self_heal", t)`; if it falls
below `h_self`, the case recovers on its own.

**Retry success:**

```
P(success | retry at t, attempt n) =
    retry_success_base(reason)
  · time_factor(t, reason)
  · attempt_decay(n)
  · intent_to_pay
```

- `time_factor` is **0** before `min_retry_wait_h` — a retry inside the cooling
  window cannot succeed, it only burns a fee. For `insufficient_funds` it ramps
  with a payroll-cycle bump near month boundaries.
- `attempt_decay` follows the published shape: the first retry recovers roughly
  40–60% of recoverable soft declines, the second another 15–25%, the third
  10–15%. Multipliers ≈ `[1.00, 0.42, 0.25, 0.15, …]`, sharply diminishing.
- Hard and action declines have `retry_success_base = 0.0`. No amount of
  retrying fixes a stolen card.

**Contact response:**

```
P(pays within 48h | contact via c at hour h) =
    responsiveness
  · channel_affinity[c]
  · quiet_hours_penalty(h)
  · fatigue_penalty(k)          # k = contacts already sent to this customer
```

### 3.4 The honesty this project is actually built to demonstrate

**The measured lift is only as good as the outcome model above.** A reviewer's
first and most obvious objection is: *you invented your own success
probabilities, so of course your agent wins.* That objection is correct and must
be met head-on rather than buried.

The README says so in its first screen: **this is a simulation result under a
published generative model, not live-money evidence.** Three things convert that
concession into a strength:

1. **The generative model is published in full.** Every parameter in
   `declines.py` and `cohort.py` is visible, commented with its provenance, and
   marked where it is our own prior rather than an external anchor.
2. **Bootstrap 95% confidence intervals** on incremental lift, using a *paired*
   bootstrap (resample case indices, recompute all arms on the same resample).
3. **A sensitivity sweep** varying `p_self_heal` by ±50% and the attempt-decay
   curve across plausible ranges, reporting whether the **sign** of the lift
   survives.

And the commitment that matters most: **if the agent only narrowly beats the
naive baseline, we report that.** Per the brief, that finding honestly reported
is worth more than an inflated one — and it is the exact trait five independent
artifacts (the four track bars, the "what broke" field, and the JD's "understand
root causes when targets are not met") converge on screening for.

---

## 4. Domain model

### 4.1 Decline taxonomy — `recovery/declines.py`

Three classes, not two. This is the distinction that proves the system was
built by someone who has read a decline report:

| Class | Meaning | Correct response |
|---|---|---|
| **SOFT** | Transient — no funds, issuer down, timeout | Retry, correctly timed |
| **HARD** | Permanent — stolen card, bad account number, invalid VPA | **Never retry.** Retrying burns a fee *and* trips issuer fraud heuristics, degrading the approval rate on your good transactions |
| **ACTION** | Instrument needs the customer to change something — expired card, revoked mandate, failed 3DS | Silent retry can never succeed. Contact is the only move |

The ACTION class is a refinement on the brief's binary hard/soft split, and it
matters: it is a large slice of real recurring-payment failures where the naive
system retries forever and the merely-competent system gives up. The right
answer is neither.

Each reason carries: `klass`, `min_retry_wait_h`, `retry_success_base`,
`self_heal_rate`, `population_weight`, and applicable payment methods.

Note that ACTION reasons have `retry_success_base = 0.0` but **non-zero**
`self_heal_rate` — customers do notice a dead subscription and update the card
unprompted. That non-zero self-heal is exactly what inflates naive gross-recovery
numbers, so modelling it is load-bearing rather than decorative.

Provenance is stated in the module docstring: reason codes are modelled on
Razorpay's `error.reason` field combined with card-network decline categories.
Razorpay does not publish a single exhaustive enumeration across all payment
methods, so this is a documented approximation, not a transcription. Population
weights are our own priors — stated openly and swept in the sensitivity
analysis. The one external anchor: insufficient funds is consistently reported
as roughly 44% of card-not-present issuer declines.

Unknown reason codes raise. Silently defaulting an unrecognised reason to
"soft" is exactly how a dunning system starts retrying stolen cards.

### 4.2 Case schema — `recovery/cohort.py`

```python
@dataclass(frozen=True)
class Case:
    case_id: str
    customer: CustomerProfile
    payment_id: str
    amount_paise: int              # integer paise everywhere; never float rupees
    method: Method                 # card | upi | netbanking | emandate | wallet
    decline_reason: str
    failed_at: datetime            # IST
    is_recurring: bool
    mandate_first_debit: bool      # AFA required regardless of amount
    mandate_category: str          # standard | insurance | mutual_fund | credit_card_bill
    latents: Latents               # HIDDEN from every arm
```

**All money is integer paise.** Floating-point rupees in a payments system is a
correctness bug waiting to happen and an instant credibility loss.

Cohort defaults: 500 cases (configurable via `--n`), seed 42, committed to
`data/cohort_seed42.jsonl` so a reviewer gets byte-identical input.

Realistic Indian distributions: UPI-heavy method mix, log-normal amounts with
mass in the ₹200–₹5,000 range plus a subscription cluster, customer segments
(new / regular / loyal) driving `intent_to_pay` and `responsiveness`.

### 4.3 Audit ledger — `recovery/ledger.py`

Append-only JSONL. One entry per policy evaluation, action, and outcome:

```python
@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    prev_hash: str            # hash chain — tamper-evidence
    entry_hash: str
    ts: datetime              # simulated clock
    run_id: str
    arm: str
    case_id: str
    kind: str                 # policy_check | action | outcome | observation
    action: dict | None
    verdicts: list[dict]      # EVERY rule verdict, pass and fail
    allowed: bool | None
    idempotency_key: str | None
    cost_paise: int
    result: dict | None
```

Two design points worth defending on video:

- **Every evaluation is logged, allow and deny.** The denials are the
  blocked-action log. An audit trail that only records what happened is half an
  audit trail; what the system *refused to do* is the interesting half.
- **Hash chain.** Each entry carries the hash of its predecessor. Cheap to
  implement, and it makes "append-only" a structural property rather than a
  claim. This is the kind of detail that reads as payments-adjacent thinking.

---

## 5. Policy engine — the differentiator

`recovery/policy/`. Every rule returns
`Verdict(allow | deny, rule_id, reason, evidence)`. Every money or contact
action routes through `PolicyEngine.evaluate(action, case, ledger, clock)`,
which runs **all** rules (not short-circuiting) so the ledger records the full
verdict set.

| Rule | Constraint | Why |
|---|---|---|
| `HardDeclineNoRetry` | Never retry a HARD-class decline | Burns fees and trips issuer fraud flags, degrading approval rate on good transactions |
| `ActionDeclineNoRetry` | Never silently retry an ACTION-class decline | Cannot succeed by construction; contact instead |
| `AttemptCap` | Stop when marginal expected recovery < marginal cost | Derived from economics, not a magic number — and we can say why |
| `CoolingOff` | Enforce `min_retry_wait_h`, payroll-aligned 24–72h for `insufficient_funds` | The largest decline bucket; retrying an empty account sooner is pure waste |
| `ContactFrequency` | Max contacts per customer per rolling window | Fatigue is a real cost, not a free action |
| `QuietHours` | No SMS/WhatsApp 21:00–09:00 IST | TRAI-aligned; dunning to Indian consumers is its own regulated surface |
| `BudgetCeiling` | Cumulative spend on a case never exceeds its expected recovery | Recovery that costs more than it recovers is a loss dressed as a win |
| `RBIPreDebitNotice` | E-mandate debit requires notification **≥24h prior**, carrying merchant name, amount, date/time, and mandate reference | RBI Digital Payments — E-mandate Framework, 2026 |
| `RBIAdditionalFactorAuth` | AFA required above **₹15,000** (₹1,00,000 for insurance / mutual funds / credit-card bills), **and mandatorily on the first debit under a mandate regardless of amount** | Same framework |
| `RBIPostDebitNotice` | Post-transaction notification must include grievance-redressal details | Same framework |
| `Idempotency` | The same `(case, action, key)` can never execute twice | The bug that double-charges a customer |
| `TerminalState` | No action on a recovered, closed, or refunded case | The bug that dunts someone who already paid |

### RBI provenance

The framework was notified **21 April 2026** and took effect immediately. It
consolidates prior e-mandate circulars and makes **acquirers responsible for
their merchants' compliance** — which is to say, Razorpay's problem, not just
the merchant's.

Verified against current sources during planning. The **first-debit AFA
requirement regardless of amount** is a rule the research brief missed and is
worth encoding explicitly. `rbi.py` cites its sources inline, at the rule.

`tests/test_policy_rbi.py` is written **before** the rules it covers. Those
tests are the compliance evidence — "compliant escalation" stops being a word
in a rubric and becomes a test in the repo.

### Keep compliance as seasoning

A panel of engineers will be impressed that we knew the regulation exists and
encoded it. They will glaze over if the pitch becomes a compliance lecture.
Budget roughly **30 seconds** of video: show one blocked action, name the rule,
move on. The measured recovery number stays the headline.

---

## 6. Agent layer

`recovery/agent/`. Model `claude-opus-5`, adaptive thinking, prompt caching on
the stable system + policy prefix (volatile per-case state goes after the last
cache breakpoint).

### Tool surface

All decorated with `@beta_tool`; all money and contact tools gate internally.

| Tool | Gated | Purpose |
|---|---|---|
| `get_case_detail(case_id)` | no | Decline reason, amount, method, attempt history |
| `get_customer_history(customer_id)` | no | Prior failures, recoveries, contacts received |
| `check_policy(action)` | no | **Read-only preview** — lets the model plan instead of trial-and-erroring against denials |
| `schedule_retry(case_id, at, idempotency_key)` | **yes** | Attempt a re-debit |
| `send_message(case_id, channel, template, at, idempotency_key)` | **yes** | SMS / WhatsApp / email nudge |
| `close_case(case_id, reason)` | no | Terminal — stop working this case |

`check_policy` is worth calling out: giving the model a dry-run gate means the
sensible path is *plan within the constraints*, and a denial on a real action
becomes a genuine surprise worth logging rather than routine noise.

### Cassettes — the reproducibility win

Every LLM decision is recorded to `data/cassettes/` keyed by a hash of the
request. `make eval` replays from cassettes by default, so:

- The published numbers reproduce **exactly**, offline, with **no API key**.
- A reviewer can clone the repo and rerun it without credentials.
- Iteration during the build is free after the first recorded run.

This is the single strongest reproducibility signal available in a submission
like this one, and it directly serves the brief's requirement that someone must
be able to clone and rerun you.

`--record` re-records; `--live` bypasses cassettes entirely.

### Cost control

`--n` bounds cohort size. Prompt caching on the stable prefix. Cassette replay
means the expensive path runs once per design change, not once per test run.

---

## 7. Metrics — `recovery/eval/metrics.py`

Let `R_a` be gross rupees recovered in arm `a`, `N` the cohort size, `C_a` the
cost incurred.

| Metric | Formula | Note |
|---|---|---|
| Gross recovered | `R_a = Σ amount_i · 1[recovered_i]` | The number everyone else reports |
| **Incremental recovery** | `Δ_a = R_a − R_control` | **The headline** |
| Recovery rate | `n_recovered_a / N` | |
| Incremental rate | `(n_recovered_a − n_recovered_control) / N` | |
| **Cost per incremental ₹** | `C_a / Δ_a` | Denominator is *incremental*, not gross. Using gross flatters you and is the same error as reporting gross recovery |
| Contact fatigue | mean and P95 contacts per customer | |
| Blocked actions | count by `rule_id` | Straight from the ledger |
| **Time-to-cash** | mean days from failure to recovery | Real value even when both arms recover: the same rupee nine days earlier is worth something in working capital |
| Bootstrap CI | paired, B = 2000, percentile method | See below |

**Paired bootstrap.** Because arms share a cohort, we resample *case indices*
with replacement and recompute every arm on the same resample, then take the
2.5th and 97.5th percentiles of `Δ`. Resampling arms independently would throw
away the pairing that common random numbers bought us and inflate the interval.

**Cost assumptions** (stated in the README, swept in sensitivity): retry
attempt ≈ ₹3, SMS ≈ ₹0.20, WhatsApp ≈ ₹0.35, email ≈ ₹0.01. These are our
priors, not quoted rates.

---

## 8. Repository layout

```
razorpay/
├── README.md                    # judged artifact — results table + caveats above the fold
├── ARCHITECTURE.md              # the write-up deliverable
├── WHAT_BROKE.md                # drafted as incidents happen, not reconstructed at the end
├── Makefile                     # delegates to the CLI
├── pyproject.toml
├── .env.example
├── docs/
│   └── prompts/
│       └── track-03-build-plan.md      # this document
├── data/
│   ├── cohort_seed42.jsonl      # committed — reviewers get identical input
│   ├── cassettes/               # recorded LLM decisions → offline reproduction
│   └── results/                 # committed run outputs + figures
├── recovery/
│   ├── cli.py                   # primary entry point
│   ├── declines.py              # decline taxonomy
│   ├── cohort.py                # L1 generator + latent outcome model
│   ├── scoring.py               # L2 P(recover | intervention), expected value
│   ├── ledger.py                # L6 append-only hash-chained audit ledger
│   ├── policy/
│   │   ├── engine.py            # L3 evaluate() → Decision
│   │   ├── rules.py
│   │   └── rbi.py               # RBI E-mandate Framework 2026
│   ├── agent/
│   │   ├── tools.py             # L4 gated @beta_tool functions
│   │   ├── runner.py            # tool_runner loop + cassette record/replay
│   │   └── prompts.py
│   ├── transport/
│   │   ├── base.py              # interface
│   │   ├── mock.py              # L5 default — deterministic simulator
│   │   └── razorpay_test.py     # L5 live test mode (day 5, optional)
│   ├── arms/
│   │   ├── control.py           # do nothing
│   │   ├── baseline.py          # Razorpay's T+3 default
│   │   └── agent.py             # policy-gated agent
│   └── eval/
│       ├── harness.py           # L7 three-arm runner
│       ├── metrics.py
│       └── report.py            # markdown + matplotlib figures
├── tests/
│   ├── test_policy_rbi.py       # these tests ARE the compliance evidence
│   ├── test_policy_declines.py
│   ├── test_idempotency.py
│   ├── test_cohort.py           # determinism: same seed → same cohort
│   └── test_metrics.py
└── scripts/
    └── make_figures.py
```

### Entry point

`make` is **not installed** on this machine, so the primary interface is the
Python CLI and the Makefile is a thin delegation layer. Reviewers on Linux or
macOS still get `make eval`; the build works here regardless.

```bash
python -m recovery.cli eval --seed 42 --n 500 --arms control,baseline,agent
python -m recovery.cli report
python -m recovery.cli sweep --param p_self_heal --range 0.5,1.5
```

### Dependencies — reuse, don't hand-roll

| Need | Use | Not |
|---|---|---|
| Agent loop | `@beta_tool` + `client.beta.messages.tool_runner` | A hand-written `while stop_reason == "tool_use"` loop |
| Razorpay test mode | `razorpay` pip SDK | Raw `requests` |
| Seeded RNG, bootstrap | `numpy` | Hand-rolled PRNG |
| Figures | `matplotlib` | — |
| Ledger | stdlib `dataclasses` + `json` | An ORM or a database |

---

## 9. Schedule — 7 days, submit 4 September

The deliberate ordering choice: **the evaluation harness is built before the
agent.** If we run out of time, we would rather have a rigorous measurement of a
simple policy than an unmeasured clever agent. Every published bar rewards the
former.

### Day 1 — Friday 29 August
**Skeleton, taxonomy, cohort, ledger, harness stub**

- `pyproject.toml`, `.gitignore`, `.env.example`, Makefile, CLI skeleton
- `declines.py` — full three-class taxonomy
- `cohort.py` — generator, latent model, common-random-numbers helper
- `ledger.py` — append-only hash-chained ledger
- `transport/mock.py` — deterministic simulator
- `arms/control.py` + a no-op arm
- `eval/harness.py` — three-arm loop
- `tests/test_cohort.py` — determinism

**Exit criterion:** `python -m recovery.cli eval` runs end to end against a
do-nothing agent and prints a results table. Today.

### Day 2 — Saturday 30 August
**Policy engine, RBI rules, real baseline**

- Policy tests written **first** (`test_policy_rbi.py`, `test_policy_declines.py`)
- `policy/engine.py`, `rules.py`, `rbi.py`
- `arms/baseline.py` — Razorpay's T+3 default (retry once a day for three days)
- Control and baseline arms now real

**Exit criterion:** a genuine incremental number for baseline-over-control. It
will be unimpressive. That is the floor, and having it on day 2 is the point.

### Day 3 — Sunday 31 August
**Agent layer**

- `agent/tools.py`, `runner.py`, `prompts.py`
- Tool calls routed through the policy gate; every refusal logged with reason
- Cassette record/replay

**Exit criterion:** first agent-arm number, and a blocked-action log with real
entries in it. **Needs `ANTHROPIC_API_KEY`.**

### Day 4 — Monday 1 September
**Measurement rigour**

- Paired bootstrap CIs
- Sensitivity sweep over `p_self_heal` and the attempt-decay curve
- `eval/report.py` — markdown results table + figures
- Time-to-cash metric

**Exit criterion:** `report` produces the table and charts that go in the README
and on screen in the video.

### Day 5 — Tuesday 2 September
**Live integration, then break it on purpose**

- `transport/razorpay_test.py` — payment links, orders, subscription
  "Charge this now", webhooks. **Needs Razorpay test keys.**
- Adversarial passes:
  - duplicate webhook delivery
  - customer pays mid-sequence
  - a hard decline mislabelled as soft
  - an agent that tries to exceed budget
  - clock skew across the 24h RBI notice boundary

**Exit criterion:** each scenario either handled or logged as a known gap in
`WHAT_BROKE.md`. Every one survived is video material.

### Day 6 — Wednesday 3 September
**The judged artifacts**

- `README.md` — results table and honest caveats above the fold
- `ARCHITECTURE.md` — the seven layers, and the two departures from the obvious
  approach with reasoning
- `WHAT_BROKE.md` — finalised from incidents recorded as they happened
- Clean-clone reproduction check

**Exit criterion:** a stranger can clone, install, run, and get the published
numbers.

### Day 7 — Thursday 4 September
**Video and submit**

Structure, per the brief:

| Time | Content |
|---|---|
| 45s | Problem, with a real number |
| 60s | Architecture |
| 90s | Live run |
| 60s | Results — **including where it underperformed** |
| 30s | One blocked action and the rule behind it |

Two rules. **Show the failing case** — every bar rewards honesty, and a demo
with no failure reads as a demo with a hidden failure. And **put the number on
screen as a number**, with the control arm beside it, not narrated over a
scrolling terminal.

Submit on the 4th. The 5th is buffer, not plan.

### Critical path

Strict dependency order means **the repo is submittable from day 3 onward** —
each subsequent day strengthens it rather than being required for coherence.
Day 5 is the only credential-dependent day, and its failure mode is a weaker
demo, not a broken submission.

---

## 10. Verification

| Command | Checks |
|---|---|
| `make test` | Policy rules, RBI constraints, idempotency, metrics maths, cohort determinism |
| `make eval` | Full three-arm run on the committed seed — must reproduce published numbers **exactly, offline, from cassettes, with no API key** |
| `make repro` | Clean clone into a temp dir, install, run, diff against `data/results/`. Any drift fails loudly |
| `make sweep` | Sensitivity analysis; the **sign** of the incremental lift must be stable across `p_self_heal` ±50% |

If the sign is *not* stable, that goes in the README as a stated limitation
rather than being quietly dropped. That outcome is a finding, not a failure.

The day 6 clean-clone check is the real gate: the README claim and the actual
output must match, byte for byte.

---

## 11. Risks and open questions

| Risk | Severity | Mitigation |
|---|---|---|
| No `ANTHROPIC_API_KEY` by day 3 | **High** — blocks the agent arm entirely | Get it now. Days 1–2 do not need it; day 3 hard-stops without it |
| No Razorpay test keys by day 5 | Medium — weaker demo only | Mock transport is the default; live integration is additive |
| Reviewer rejects the synthetic-cohort premise | Medium | Met head-on in §3.4: published generative model, paired bootstrap CIs, sensitivity sweep, and the caveat stated in the README's first screen |
| Agent barely beats baseline | Low | This is a *reportable finding*, not a failure. The brief is explicit that an honest small number beats an inflated large one |
| Deadline is not actually 5 September | Medium | **Verify on the application form.** The date comes from secondary reporting; the official page renders dynamically and did not expose it to automated fetching. Dependency ordering means an earlier deadline costs polish, not coherence |
| Decline-reason strings don't match production | Low | Provenance stated in the module docstring; nothing downstream depends on the exact strings |
| Scope creep into a dashboard or web UI | Medium | **Explicitly out of scope.** The deliverable is a number, a ledger, and a repo. A UI adds demo risk and zero rubric points |

### Owner tasks outside the code

1. **Obtain `ANTHROPIC_API_KEY`** — hard blocker from day 3.
2. **Register for Razorpay test-mode keys** — do it early; do not discover an
   onboarding wall on day 5.
3. **Confirm the deadline and the exact submission-form fields** on
   razorpay.com/buildathon.

---

## 12. Submission checklist

- [ ] Public repository, clean history, no secrets committed
- [ ] README leads with the results table and the honest caveat
- [ ] `ARCHITECTURE.md` covers all seven layers and both departures from the
      obvious approach
- [ ] `WHAT_BROKE.md` describes a real incident **with a number attached** — the
      retry storm, the idempotency bug, or the confidently-wrong scorer. Not a
      dependency conflict
- [ ] Blocked-action log visible and queryable
- [ ] Seed and cassettes committed; clean clone reproduces published numbers
- [ ] Five-minute video, structured as §9 day 7, showing a failure
- [ ] Submitted 4 September
