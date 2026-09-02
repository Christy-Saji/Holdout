# Phase 7 — Live integration and adversarial passes

> Track 03 · Razorpay AI Buildathon · rebased target **Wed 3 September**
> Master spec: `docs/prompts/track-03-build-plan.md` §2.2, §9 day 5
> Previous phase: `phase-06-measurement-rigour.md` · Next: `phase-08-artifacts-and-submission.md`
> **Optional. This is the drop-first phase.** Requires Razorpay test-mode keys.

---

## Prompt

> You are building the Razorpay AI Buildathon Track 03 submission — an AI revenue
> recovery system whose headline deliverable is **incremental** money recovered
> against a holdout control arm.
>
> This phase does two things: wires a real Razorpay test-mode transport alongside the
> mock, and then deliberately attacks the system with five adversarial scenarios to
> find out what breaks.
>
> **This phase is optional.** It is the only credential-dependent work in the project
> and its failure mode is a weaker demo, not a broken submission. If test keys have
> not arrived, skip the transport work, run the adversarial passes against the mock
> transport anyway — they are the more valuable half — and go to phase 8.
>
> The adversarial passes are not a test-hardening exercise. **They are how
> `WHAT_BROKE.md` gets written with real content**, and that document is one of the
> four judged artifacts.
>
> Read `CLAUDE.md` for the project invariants. Everything else you need is below.
>
> **Standing rule for this project: show your plan in your reply as visible text and
> wait for explicit approval before creating or editing any implementation file.**

---

## Prerequisites

- Phase 6 complete: `report` produces the results table and figures
- For the transport half only: **Razorpay test-mode keys** in `.env` as
  `RAZORPAY_KEY_ID` / `RAZORPAY_KEY_SECRET`, and `python -m pip install razorpay`
  **into the activated `.venv/`, never globally**
- The adversarial half needs **no credentials** and should be done regardless

---

## Why this phase moved, and why it is droppable

The research brief put live integration on day 4. It moved to day 5 — and here to the
second-to-last phase — because credentials were deferred and the mock transport is the
default by design. The consequences, stated plainly so nobody re-promotes this work:

- The evaluation harness never blocks on sandbox onboarding.
- **If keys never arrive, the submission still stands on its own.**
- The live integration is a *demonstration of real API wiring*, not a load-bearing
  dependency of the published results.

Nothing downstream depends on this phase. Cutting it costs a good 30 seconds of video
and one paragraph in `ARCHITECTURE.md`.

---

## Deliverables

| File | Contents | Spec |
|---|---|---|
| `recovery/transport/razorpay_test.py` | Live test-mode transport | §8 |
| `tests/test_adversarial.py` | The five scenarios | §9 day 5 |
| `WHAT_BROKE.md` | Started here, finalised in phase 8 | §12 |

---

## Specification

### `recovery/transport/razorpay_test.py`

Implements the same `Transport` protocol as `mock.py` (phase 3), so it drops in
without any arm changing. Use the **`razorpay` pip SDK**, not raw `requests`.

Surfaces to wire:

- **Payment links** — the contact-to-pay path.
- **Orders** — the retry path.
- **Subscriptions, "Charge this now"** — the e-mandate debit path, which is what the
  RBI rules actually govern.
- **Webhooks** — inbound payment-status notifications.

Test mode only. Never write live keys into the repo, and confirm `.gitignore` still
excludes `.env` before the file exists.

This transport is **not** used for the published numbers. The results in the README
come from the deterministic mock, because a network-dependent result is neither
reproducible nor offline-replayable. Say so in `ARCHITECTURE.md` — running the
integration and then *not* basing the headline on it is a deliberate choice worth
naming.

### The five adversarial passes

Run each against the mock transport at minimum. Each either gets handled or gets
logged as a known gap in `WHAT_BROKE.md`. **Every one survived is video material; every
one that breaks is better video material**, provided it is written up honestly.

#### 1. Duplicate webhook delivery

Deliver the same payment-success webhook twice. The case must not be marked recovered
twice, the ledger must not double-count the amount, and no follow-up action may fire
on the second delivery.

Real webhooks are at-least-once. A system that assumes exactly-once is wrong in
production, and this is the cheapest place to prove you knew that.

#### 2. Customer pays mid-sequence

A case recovers on its own at hour 100 while a retry is scheduled for hour 110 and an
SMS for hour 120. Both queued actions must be blocked by `TerminalState`, and the
denials must appear in the ledger.

This is **the bug that dunts someone who already paid** — the most embarrassing
failure a dunning system can have, and the one a payments engineer will look for
first.

#### 3. A hard decline mislabelled as soft

Feed a case whose `decline_reason` says `insufficient_funds` but whose underlying
truth is a stolen card. The system will retry, correctly per its inputs, and fail.

The point is not to prevent this — you cannot, from bad input. The point is to observe
what the system does with the wrong answer, whether it stops on its own via
`AttemptCap`, and how much it costs before it does. That number belongs in
`WHAT_BROKE.md`.

#### 4. An agent that tries to exceed budget

Construct a low-amount case where sustained contact would cost more than the payment
is worth. `BudgetCeiling` must deny, and the agent must react to the denial and close
the case rather than looping against the gate.

An agent that retries the same denied action until it hits the iteration cap is a real
failure mode. Check for it explicitly.

#### 5. Clock skew across the 24h RBI notice boundary

Evaluate an e-mandate debit where the pre-debit notice was sent 23h59m ago, then again
at 24h01m, then with the clock nudged across the boundary mid-evaluation. The verdict
must be deterministic and must depend only on the simulated clock passed in.

Phase 4 tested the boundary statically. This tests it under skew, which is the
version that actually bites — and a compliance rule whose verdict depends on wall
time is a compliance rule that fails an audit.

### `WHAT_BROKE.md` — start it here

The submission checklist requires a real incident **with a number attached**. Suitable
material: a retry storm, an idempotency bug, a confidently-wrong scorer, or whatever
these five passes actually turn up. **A dependency conflict does not count** and will
read as having nothing real to report.

For each incident record: what happened, the number (rupees wasted, duplicate charges,
cases affected), how it was found, what the fix was, and what it means for the design.
Write it as it happens — reconstructed incident reports read as reconstructed.

---

## Invariants this phase must not violate

- **Live keys are test-mode only, and never committed.**
- **The published numbers come from the mock transport**, not from live calls.
- **`razorpay` SDK, not raw `requests`.**
- All the standing invariants still apply to the new transport: integer paise,
  policy gate on every action, ledger append-only.

---

## Tests

`tests/test_adversarial.py` — one test per scenario above, all runnable offline
against the mock transport.

- Duplicate webhook: exactly one recovery recorded, ledger amount unchanged on the
  second delivery.
- Mid-sequence payment: both queued actions denied by `TerminalState`, both denials in
  the ledger.
- Mislabelled decline: the run terminates via `AttemptCap` rather than exhausting the
  window, and the total wasted cost is asserted to be bounded.
- Budget breach: `BudgetCeiling` denies, and the agent does not re-issue the same
  denied action more than once.
- Clock skew: the verdict at a given simulated timestamp is identical across repeated
  evaluations and across process restarts.

Live-transport tests, if keys exist, go behind a marker that skips without
credentials — `pytest` must stay green for a reviewer who has no keys.

---

## Exit criteria

- [ ] Each of the five scenarios is **either handled or logged as a known gap** in
      `WHAT_BROKE.md`
- [ ] `pytest tests/test_adversarial.py` passes offline with no credentials
- [ ] `WHAT_BROKE.md` contains at least one real incident **with a number attached**
- [ ] The full suite is still green: `pytest`
- [ ] The published numbers are unchanged — this phase must not move the headline
- [ ] If keys arrived: one live test-mode payment link or order created successfully,
      and `.env` is still untracked (`git status --porcelain | grep -c '\.env'` is 0)
- [ ] If keys did not arrive: that is recorded in `WHAT_BROKE.md` or
      `ARCHITECTURE.md` as a stated scope decision, not left silent

---

## Pitfalls

- **Treating this as required.** It is not. If it is Wednesday evening and phase 8 has
  not started, stop here and go to phase 8. The judged artifacts matter more than the
  live integration.
- **Basing the headline number on live calls.** Non-reproducible, non-offline, and it
  would break the clean-clone gate in phase 8.
- **Committing `.env`.** Check before, not after.
- **Skipping the adversarial passes because the keys did not arrive.** They are the
  more valuable half and they need no credentials. This is the most likely way to lose
  the value of this phase.
- **Fixing every incident and reporting none.** The point of the exercise is
  `WHAT_BROKE.md`. An incident found, fixed, and written up with its cost is worth
  more than an incident silently patched — a demo with no failure reads as a demo with
  a hidden failure.
- **Writing the incidents up at the end from memory.** Write them as they happen.
- **Letting the agent loop against a denial.** Scenario 4 exists to catch this. If the
  agent re-issues a denied action repeatedly, that is a real finding and belongs in
  `WHAT_BROKE.md` rather than being quietly capped away.
