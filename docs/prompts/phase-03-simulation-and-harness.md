# Phase 3 — Simulation and harness

> Track 03 · Razorpay AI Buildathon · rebased target **Sat 30 August** (with phases 1 and 2)
> Master spec: `docs/prompts/track-03-build-plan.md` §3.2, §3.3, §7 (partial)
> Previous phase: `phase-02-domain-core.md` · Next: `phase-04-policy-engine-and-rbi.md`

---

## Prompt

> You are building the Razorpay AI Buildathon Track 03 submission — an AI revenue
> recovery system whose headline deliverable is **incremental** money recovered
> against a holdout control arm.
>
> This phase builds the outcome model and the measurement spine: a deterministic
> hourly simulator over a 14-day window, the do-nothing control arm, the three-arm
> evaluation harness, and a first cut of the metrics. No policy rules and no agent —
> those are phases 4 and 5.
>
> This is where the project's central claim becomes real. Every arm runs the *same*
> cohort with the *same* latents, and every stochastic draw is keyed by event
> identity, so the difference between arms is attributable purely to the intervention.
> If you break that, every number the submission publishes becomes noise.
>
> Read `CLAUDE.md` for the project invariants. Everything else you need is below.
>
> **Standing rule for this project: show your plan in your reply as visible text and
> wait for explicit approval before creating or editing any implementation file.**

---

## Prerequisites

- Phase 2 complete: `pytest tests/test_cohort.py tests/test_ledger.py` passes
- `data/cohort_seed42.jsonl` exists with 500 lines
- `recovery.declines.lookup`, `recovery.cohort.u`, `recovery.ledger` all importable
- No credentials required

---

## Deliverables

| File | Contents | Spec |
|---|---|---|
| `recovery/transport/base.py` | Transport interface every arm talks to | §8 |
| `recovery/transport/mock.py` | Deterministic 336-hour simulator | §3.3 |
| `recovery/arms/control.py` | Do-nothing holdout arm | §9 day 1 |
| `recovery/eval/harness.py` | Three-arm runner | §8 |
| `recovery/eval/metrics.py` | Gross, incremental, rate, cost-per-incremental-₹ | §7 |
| `tests/test_simulator.py` | CRN pairing and hazard properties | §10 |
| `tests/test_metrics.py` | Metric arithmetic | §10 |

---

## Specification

### The outcome model — `recovery/transport/mock.py`

A discrete hourly simulation over a **14-day (336-hour) window** per case. Let
`W = 336`.

#### Self-heal

Modelled as a **per-hour hazard** so it composes correctly with interventions rather
than being a single end-of-window coin flip:

```
h_self = 1 − (1 − p_self_heal) ** (1 / W)        # W = 336
```

At each untouched hour `t`, draw `u(seed, case_id, "self_heal", t)`; if it falls
below `h_self`, the case recovers on its own at hour `t`.

Getting this right matters more than it looks. A case that self-heals at hour 40 and
a case that self-heals at hour 300 both count as recovered, but they differ in
time-to-cash (phase 6), and a case that self-heals at hour 40 must not also be
"recovered" by a retry at hour 60 — once terminal, always terminal.

#### Retry success

```
P(success | retry at t, attempt n) =
    retry_success_base(reason)
  · time_factor(t, reason)
  · attempt_decay(n)
  · intent_to_pay
```

- **`time_factor` is 0 before `min_retry_wait_h`.** A retry inside the cooling window
  cannot succeed — it only burns a fee. For `insufficient_funds` it then ramps, with a
  payroll-cycle bump near month boundaries, because that is when Indian salary
  accounts refill.
- **`attempt_decay`** follows the published shape: the first retry recovers roughly
  40–60% of recoverable soft declines, the second another 15–25%, the third 10–15%.
  Multipliers approximately `[1.00, 0.42, 0.25, 0.15, ...]`, sharply diminishing.
- **HARD and ACTION declines have `retry_success_base = 0.0`**, so this product is
  identically zero for them. That is by construction, not a special case to code
  around.

Draw with `u(seed, case_id, "retry", attempt_number)`.

#### Contact response

```
P(pays within 48h | contact via c at hour h) =
    responsiveness
  · channel_affinity[c]
  · quiet_hours_penalty(h)
  · fatigue_penalty(k)          # k = contacts already sent to this customer
```

Draw with `u(seed, case_id, f"contact_{channel}", contact_index)`.

`quiet_hours_penalty` is the *outcome-side* consequence of contacting someone at
03:00 — a message sent then is simply less effective. It is distinct from the phase-4
`QuietHours` policy rule, which *forbids* the send. Both should exist: the rule is the
compliance story, the penalty is the economic one.

#### Determinism requirements

The simulator must be a pure function of `(cohort, seed, action_sequence)`. Same
inputs, same outcomes, every time, in any process. Concretely:

- No `random`, no `numpy.random`, no wall-clock time.
- Event kinds are fixed strings. Renaming `"self_heal"` to `"selfheal"` changes every
  draw in the project and silently invalidates all committed results — treat the
  event-kind strings as a frozen interface.
- Iterating a `dict` or `set` to decide draw order is a latent non-determinism bug.
  Sort before iterating.

### `recovery/transport/base.py`

The interface every arm codes against, so `mock.py` and phase 7's
`razorpay_test.py` are interchangeable:

```python
class Transport(Protocol):
    def attempt_retry(self, case_id: str, at_hour: int, idempotency_key: str) -> Result: ...
    def send_message(self, case_id: str, channel: str, template: str,
                     at_hour: int, idempotency_key: str) -> Result: ...
    def observe(self, case_id: str, at_hour: int) -> CaseState: ...
```

`observe` returns **only what an arm is allowed to see**: current status, attempt
history, contacts sent, decline reason, amount. **Never latents.** This is the seam
that makes the hidden ground truth structural rather than conventional.

Costs, in paise, charged by the transport and returned on each `Result` — these are
**our own priors, not quoted rates**, and phase 6 sweeps them:

| Action | Cost |
|---|---|
| Retry attempt | ₹3.00 = 300 paise |
| SMS | ₹0.20 = 20 paise |
| WhatsApp | ₹0.35 = 35 paise |
| Email | ₹0.01 = 1 paisa |

### `recovery/arms/control.py`

Does nothing. Every hour, for every case, it takes no action; the simulator runs the
self-heal hazard and reports what recovered anyway.

This arm is not a placeholder — **it is the measurement instrument.** Its recovery
number is the counterfactual that turns every other arm's gross number into an
incremental one. Give it the same ledger treatment as the others so the run
artifacts are symmetric.

Define the arm interface here too (all arms implement it):

```python
class Arm(Protocol):
    name: str
    def act(self, state: CaseState, hour: int, transport: Transport) -> list[Action]: ...
```

### `recovery/eval/harness.py`

Loads the cohort, runs each named arm over the full 336-hour window for every case,
writes one ledger per `(run_id, arm)`, and collects per-case outcomes.

- **Arms must not share mutable state.** Each gets a fresh transport instance seeded
  identically. Cross-contamination between arms is the failure mode that produces a
  beautiful, wrong number.
- Assign a `run_id` once and thread it through every ledger entry.
- Persist per-case outcomes to `data/results/<run_id>/<arm>.jsonl` so phase 6 can
  bootstrap without re-running.
- `--arms` defaults to `control` alone in this phase; `baseline` arrives in phase 4
  and `agent` in phase 5.

### `recovery/eval/metrics.py` — v0

Let `R_a` be gross paise recovered in arm `a`, `N` the cohort size, `C_a` the cost
incurred.

| Metric | Formula | Note |
|---|---|---|
| Gross recovered | `R_a = Σ amount_i · 1[recovered_i]` | The number everyone else reports |
| **Incremental recovery** | `Δ_a = R_a − R_control` | **The headline** |
| Recovery rate | `n_recovered_a / N` | |
| Incremental rate | `(n_recovered_a − n_recovered_control) / N` | |
| **Cost per incremental ₹** | `C_a / Δ_a` | Denominator is *incremental*, not gross |
| Blocked actions | count by `rule_id`, straight from the ledger | Empty until phase 4 |

**Cost per incremental rupee has an incremental denominator.** Using gross flatters
the result and is the same error as reporting gross recovery in the first place. If
`Δ_a <= 0`, the ratio is undefined — return `None` and let the report say so. Do not
return infinity, and do not silently fall back to the gross denominator.

Bootstrap CIs, time-to-cash and contact fatigue are **phase 6**. Leave them out
rather than stubbing them.

Print a results table with one row per arm and the control row first, since every
other row is read as a difference from it.

---

## Invariants this phase must not violate

- **Every draw goes through `u()`** from `recovery/cohort.py`. No `random`, no
  `numpy.random`, no sequential streams.
- **Latents never leave the simulator.** `observe()` must not expose them, and no arm
  may import them.
- **All money is integer paise.** Costs, amounts, and totals.
- **The ledger `ts` is the simulated clock.** Never wall-clock.
- **Terminal is terminal.** A recovered, closed or refunded case takes no further
  action and cannot recover twice.
- **Cost per incremental rupee uses the incremental denominator.**

---

## Tests

### `tests/test_simulator.py`

- **The CRN pairing test, which is the important one.** Run the control arm and a
  no-op arm that takes no actions but *does* call `observe()` every hour. Every case
  must self-heal at exactly the same hour in both. If observation perturbs outcomes,
  the draw keying is wrong.
- Running the same arm twice gives byte-identical per-case outcomes.
- A retry inside `min_retry_wait_h` never succeeds, for every SOFT reason.
- A retry on any HARD or ACTION reason never succeeds, at any hour, at any attempt.
- The self-heal hazard integrates to approximately `p_self_heal` over 336 hours —
  simulate a large cohort with a fixed `p_self_heal` and check the realised rate is
  within tolerance. This catches an inverted or mis-rooted hazard formula.
- `attempt_decay` is monotonically decreasing.
- Arm execution order does not change any arm's result: run `[control, noop]` then
  `[noop, control]` and compare.

### `tests/test_metrics.py`

- Incremental recovery of the control arm against itself is exactly 0.
- Cost per incremental rupee returns `None` when `Δ <= 0`, and is not infinity.
- Hand-computed fixture: three cases with known amounts and known recovery flags
  produce the expected gross and incremental figures.
- Amounts stay `int` through every aggregation.

---

## Exit criteria

- [ ] `python -m recovery.cli eval --seed 42 --n 500` runs end to end and prints a
      results table — **this is the master plan's original Day 1 exit criterion**
- [ ] The control arm reports a **non-zero** gross recovery. If it is zero, the
      self-heal hazard is not firing and the whole measurement design is inert
- [ ] `pytest` passes, including the CRN pairing test
- [ ] Two consecutive runs write identical `data/results/<run_id>/control.jsonl`
      contents (modulo `run_id`)
- [ ] `python -c "from recovery.ledger import verify; print(verify('data/results/.../control.jsonl'))"`
      returns `True`
- [ ] `grep -rn "random\." recovery/` returns nothing outside comments

---

## Pitfalls

- **A zero-recovery control arm looks like a clean result and is a broken one.** The
  control arm recovering money on its own is the entire point — it is what makes
  gross numbers misleading and incremental numbers necessary. If control recovers
  nothing, check the hazard root: `(1 - p) ** (1/W)`, not `p / W`.
- **Reusing a sequence number across event kinds.** `u(seed, case, "retry", 1)` and
  `u(seed, case, "self_heal", 1)` are independent by design — but reusing the same
  `sequence` counter across *the same* event kind for different purposes silently
  correlates them.
- **Letting an arm see the future.** `observe(case, hour)` must reflect state as of
  that hour only. Returning the final outcome, or any latent, produces an arm with
  oracle powers and a result that means nothing.
- **Mutable transport shared across arms.** Fresh instance per arm, or arm two
  inherits arm one's attempt history.
- **Dict/set iteration order deciding draw order.** Sort first.
- **Stubbing phase 6 metrics.** An empty `bootstrap_ci()` that returns `(0, 0)` will
  be forgotten and will end up in the README. Leave the function out entirely.
- **Do not build the policy engine here.** The control arm takes no actions, so it
  needs no gate. Adding a permissive stub gate now means phase 4 has to unpick it.
