# Architecture

Seven layers, and the three places this system deliberately does **not** do the
obvious thing. The departures are the part worth reading; the reasoning for each is
stated rather than implied.

---

## The seven layers

| L | Layer | Module | Responsibility |
|---|---|---|---|
| L1 | Cohort and latent ground truth | `cohort.py`, `declines.py` | Generates the synthetic population and the hidden state that decides outcomes |
| L2 | Scoring | `scoring.py` | Estimates P(recover \| intervention) and expected value — from observables only |
| L3 | Policy engine | `policy/` | Twelve rules, nine operational and three RBI; every action passes through it |
| L4 | Agent | `agent/` | A policy-gated tool-runner loop over six tools |
| L5 | Transport | `transport/` | The deterministic 336-hour outcome model behind a swappable interface |
| L6 | Audit ledger | `ledger.py` | Append-only, hash-chained record of every verdict and action |
| L7 | Evaluation harness | `eval/` | Three-arm runner, metrics, bootstrap intervals, sweep, reproduction gate |

The data flow is one-directional and the seams are deliberate:

```
L1 cohort ──(latents)──> L5 transport ──(CaseState, no latents)──> arms
                                │                                    │
                                │                              L2 scoring
                                │                                    │
                                └────── L3 policy engine <───────────┘
                                             │
                                       L6 ledger (every verdict)
                                             │
                                       L7 harness → metrics
```

### L1 — Cohort and latent ground truth

`cohort.py` generates 500 synthetic subscription failures from a seed. Each case
carries **latents** — true intent to pay, per-channel affinity, responsiveness, and
self-heal propensity — written to a side table that the simulator reads and nothing
else does.

`declines.py` is the decline taxonomy. Each reason code carries a class, a base retry
success rate, a minimum retry wait, and a self-heal rate. **An unknown reason code
raises.** It never defaults to `SOFT`, because defaulting to soft is how a dunning
system starts retrying stolen cards.

All randomness comes from one helper:

```python
u(seed, case_id, event_kind, sequence) -> float   # blake2b, uniform [0,1)
```

Keyed by event *identity*, never by stream position. This is what makes the arms
comparable — see the third departure below.

### L2 — Scoring

`scoring.py` estimates recovery probability and expected value from a `CaseState`.
It takes no `Case` and imports no `Latents`; there is structurally no field for a
latent to arrive in.

**It is deliberately wrong in three documented ways**, and its docstring says so: it
assumes uniform willingness to pay, its attempt-decay curve differs from the
simulator's, and it has no payroll-cycle term. A scorer whose model matched the
generative model would be an oracle, and the number it produced would mean nothing.
The gap between what the simulator knows and what the scorer guesses is the thing
being measured.

### L3 — Policy engine

`policy/engine.py` exposes one entry point, `evaluate()`, returning a `Decision`
carrying a `Verdict` from every rule. Nine operational rules in `rules.py`
(`TerminalState`, `Idempotency`, `HardDeclineNoRetry`, `ActionDeclineNoRetry`,
`CoolingOff`, `AttemptCap`, `ContactFrequency`, `QuietHours`, `BudgetCeiling`) and
three RBI rules in `rbi.py` with inline citations (`RBIPreDebitNotice`,
`RBIAdditionalFactorAuth`, `RBIPostDebitNotice`).

Two properties matter more than the rule list:

**Every money or contact action routes through `evaluate()`.** There is no bypass in
a test helper and no debug path around it.

**The engine does not short-circuit.** All twelve rules run on every action even once
one has denied it, and the ledger records every verdict, pass and fail. A
short-circuiting engine would produce a cheaper evaluation and a useless audit log —
you would know an action was blocked but not everything that was wrong with it. The
denials *are* the blocked-action log; 628 of them in the published baseline run.

Rule order does not change `allowed` (it is a conjunction) but it does fix which
denial `first_denial()` reports back to the agent, so structural refusals precede
economic ones and the model gets the most actionable reason first.

**Why an RBI layer belongs in a recovery system at all.** The recurring-payments
framework binds the *acquirer* — the regulated entity processing the mandate — and not
only the merchant whose subscription it is. A dunning system that retries an e-mandate
debit without the 24-hour pre-debit notice, or that debits above the AFA threshold
without additional-factor authentication, does not merely expose its merchant: it puts
the acquirer in breach of an obligation the acquirer itself carries. That is why these
three rules sit in the same non-bypassable engine as the economic ones rather than in a
compliance checklist somewhere upstream, and why their denials are recorded rather than
merely enforced.

### L4 — Agent

`agent/tools.py` defines six `@beta_tool` functions; three of them call the policy
engine internally and return a refusal the model must react to, rather than raising.
`agent/runner.py` drives `client.beta.messages.tool_runner` and handles cassette
record/replay. `agent/prompts.py` holds a frozen system prompt behind a cache
breakpoint.

**Status: built and tested, never run against the live API.** Recording cassettes
needs an `ANTHROPIC_API_KEY` that was not available before submission, so there is no
agent-arm number in the results. `tests/test_agent_gating.py` covers the layer with 23
tests that pass with no key present — including that a denied tool never reaches the
transport, that a denial writes one policy-check entry with a populated `rule_id` and
no action, and that a recorded plan replays to identical actions. The harness treats
arms uniformly, so the arm slots in the moment cassettes exist.

### L5 — Transport

`transport/base.py` is a `Protocol` with three methods — `attempt_retry`,
`send_message`, `observe` — plus `CaseState`, the only view an arm ever gets. It
carries status, attempt history, contacts sent, decline reason and amount. It cannot
carry a latent, and it cannot see a future hour.

`transport/mock.py` is the outcome model: a discrete hourly simulation over 336 hours.
Self-heal is a **per-hour hazard**, `1 - (1 - p) ** (1/336)`, so it composes with
interventions rather than being a single end-of-window coin flip. Retry success is the
product of a base rate by reason, a time factor (zero inside the cooling window, with
a payroll-cycle bump for `insufficient_funds` at month boundaries), an attempt-decay
multiplier, and the customer's latent intent. Contact response is responsiveness ×
channel affinity × quiet-hours penalty × fatigue.

Terminal is terminal: a case that self-heals at hour 40 cannot also be recovered by a
retry at hour 60.

`transport/razorpay_test.py` is the same Protocol against a real Razorpay **test-mode**
account: Orders for the card and UPI retry path, `payments/create/recurring` for the
e-mandate debit the RBI rules govern, and Payment Links for the contact-to-pay path.
Because it satisfies the same Protocol, no arm changes when it is swapped in — and no
published number comes from it. See departure 2 below, and incident 6 in
`WHAT_BROKE.md` for its honest status: written, never executed.

`transport/webhooks.py` handles the inbound half, and handles it as **at-least-once**.
A duplicate delivery is not an edge case; it is what a producer sends when the first
delivery's response was lost, which is exactly the occasion on which the first delivery
did take effect. Deduplication keys on Razorpay's own `x-razorpay-event-id`, and the
recovery side effect lives in one place — `mark_recovered`, which returns whether it
changed anything — so a second delivery credits no money and fires no follow-up. The
duplicate is still written to the ledger, carrying `applied: false` and a zero amount:
an audit trail that silently drops redeliveries cannot answer the first question asked
when a customer disputes a double debit.

### L6 — Audit ledger

`ledger.py` appends JSON lines, each carrying `prev_hash` and `entry_hash`, forming a
chain. There is **no update path and no delete path** in the module — not a
convention, an absence. `verify()` walks a file and re-derives the chain.

Timestamps are the simulated clock threaded through from the harness. There is no
`datetime.now()` anywhere in the runtime path.

### L7 — Evaluation harness

`eval/harness.py` runs each named arm over the same cohort against its **own**
transport instance — a shared transport would let arm two inherit arm one's attempt
history, which is the failure mode that produces a beautiful, wrong number. One
ledger per `(run_id, arm)`.

`eval/metrics.py` computes gross, incremental, rates, and cost per incremental rupee —
with an **incremental denominator**, because dividing cost by gross recovery flatters
the result in exactly the way this project exists to criticise. When the delta is not
positive the ratio is `None`, never infinity and never a silent fallback to gross.

**Why incremental rather than gross, stated plainly.** Gross recovery counts every
payment that would have arrived anyway. In this model 20.2% of failed payments heal
unaided, so the baseline arm's gross ₹166,551 is 2.0× the ₹82,931 it actually caused —
a system reporting the gross figure would be claiming credit for the control arm's work.
Incremental recovery is the only figure that survives the question *compared to what?*,
and answering that question is the entire reason a holdout arm exists in a repository
that could have shipped a larger number without one.

**Why common random numbers.** Both arms run the same cohort with the same hidden
latents, and every stochastic draw is keyed by `(seed, case_id, event_kind, sequence)`
through a blake2b helper rather than drawn from a sequential stream. A sequential RNG
would desynchronise the arms the moment one of them made one extra draw, so the
difference between arms would carry the noise of two independent runs. The pairing is
what makes the difference measurable at this cohort size — the paired bootstrap interval
is materially narrower than the unpaired one, and `tests/test_metrics.py` asserts that
rather than assuming it.

`eval/metrics.py` also reports **time-to-cash** (mean days from failure to recovery,
excluding non-recoveries rather than scoring them as day zero) and **contact fatigue**
(mean and P95 contacts per customer, aggregated per customer because it is the customer
who receives the messages, not the case).

`eval/report.py` adds the paired bootstrap and the sensitivity sweeps.
`eval/repro.py` is the reproduction gate.

---

## The departures

### 1. Anthropic SDK tool runner, not the Claude Agent SDK

The obvious choice was the Claude Agent SDK, on the grounds that Razorpay's Agent
Studio is built on it. That was considered and reversed.

The Claude Agent SDK is Claude Code packaged as a library: built-in Read/Write/Edit/
Bash tools, a filesystem harness, context management for coding work. Using it to
decide whether to send an SMS at 14:00 means spinning up a coding harness to make a
scheduling decision, and invites an obvious question with no good answer.

The right surface is `client.beta.messages.tool_runner` with `@beta_tool`-decorated
functions: an agentic loop over tools *we* define, no built-in tools, no sandbox. And
Anthropic's own guidance puts approval gating **inside the tool function** — return a
refusal the model has to react to — which is exactly the seam this system needs. The
policy engine sits in that seam, and the blocked-action log falls out of it for free
rather than being bolted on.

### 2. The mock transport is the default; live integration is additive

Every arm runs against a deterministic mock. The evaluation harness never depends on
the network or on credentials, which is what lets `python -m recovery.cli repro`
regenerate every committed artifact and byte-compare it offline.

The live Razorpay test-mode transport is a *demonstration* of real API wiring, not a
load-bearing dependency of the result. `Transport` is a Protocol precisely so that
addition stays additive: `razorpay_test.py` drops in behind the same five methods and
no arm notices.

**Running the integration and then deliberately not basing the headline on it is a
choice, and it is worth naming rather than leaving as an omission.** A network result
is not reproducible — two runs a day apart differ, a reviewer without credentials
cannot run it at all, and the clean-clone gate would have nothing to byte-compare. So
the published numbers come from the deterministic simulator by design, and the live
transport exists to show the wiring is real.

In the event the keys never arrived, so it has never made a live call. That cost almost
nothing precisely because the mock was the default from phase 3 rather than a fallback
improvised when the credentials failed to appear. Incident 6 in `WHAT_BROKE.md` records
it in full.

### 3. Common random numbers, not a sequential RNG

Stated third because it is the one a reader is most likely to "fix".

Every draw is `blake2b(seed | case_id | event_kind | sequence)`. The tempting
simplification — one seeded `Generator` per run, drawing in order — looks equivalent
and is not. Under a sequential stream, the *number of draws taken before* a given
event depends on what the arm did, so the same case gets different random numbers in
different arms. The pairing between arms silently dissolves, the variance of the
difference inflates, and every published incremental number becomes noise that still
looks like a result.

Keying by event identity means a case that self-heals at hour 137 in the control arm
self-heals at hour 137 in every arm, unless an intervention reached it first. That is
what makes 500 cases enough to bound, and it is why the bootstrap in `metrics.py`
resamples *cases* carrying both arms' outcomes rather than resampling each arm
independently.

The event-kind strings are consequently a **frozen interface**. Renaming `"self_heal"`
to `"selfheal"` changes every draw in the project and invalidates every committed
number, silently.

---

## A fourth decision, smaller but load-bearing

**Three decline classes, not two.** The conventional split is hard versus soft. This
system adds **ACTION**: expired cards, revoked mandates, failed 3DS. These can never
be fixed by a silent retry — `retry_success_base` is 0.0 — but they are not
permanently dead either, because contact can fix them.

The reason this is load-bearing rather than decorative: ACTION reasons have a
**non-zero self-heal rate**. Customers update expired cards on their own. That
self-healing is precisely what inflates a naive gross-recovery number, and modelling
it is what gives the control arm ₹83,620 of recovery to subtract.

---

## What this architecture buys

- Any arm can be added without touching the measurement.
- The measurement cannot be gamed by an arm, because latents are unreachable from
  `CaseState` by construction rather than by discipline.
- Every action taken and every action *refused* is on a hash-chained record.
- The whole result regenerates from a seed, offline, with no credentials, and
  `repro` byte-compares it.

The costs are stated in the README's limitations section. The largest is that the
cohort is synthetic, which is why `sweep` exists and why the headline is reported with
an interval rather than as a point.
