# Phase 4 — Policy engine and RBI rules

> Track 03 · Razorpay AI Buildathon · rebased target **Sun 31 August**
> Master spec: `docs/prompts/track-03-build-plan.md` §5
> Previous phase: `phase-03-simulation-and-harness.md` · Next: `phase-05-agent-layer.md`

---

## Prompt

> You are building the Razorpay AI Buildathon Track 03 submission — an AI revenue
> recovery system whose headline deliverable is **incremental** money recovered
> against a holdout control arm.
>
> This phase builds the differentiator: a policy engine that gates every money and
> contact action, twelve rules including three drawn from the RBI E-mandate Framework
> 2026, and the real T+3 baseline arm. Track 03's published bar asks for "compliant
> escalation, stopping rules, and an audit trail" — this phase is where all three
> stop being words in a rubric and become code and tests.
>
> **Write the tests first.** `tests/test_policy_rbi.py` and
> `tests/test_policy_declines.py` are written before the rules they cover, because
> those tests *are* the compliance evidence a reviewer will look for.
>
> Read `CLAUDE.md` for the project invariants. Everything else you need is below.
>
> **Standing rule for this project: show your plan in your reply as visible text and
> wait for explicit approval before creating or editing any implementation file.**

---

## Prerequisites

- Phase 3 complete: `python -m recovery.cli eval --seed 42 --n 500` prints a results
  table and the control arm reports non-zero gross recovery
- `pytest` passes, including the CRN pairing test
- No credentials required

---

## Deliverables

| File | Contents | Spec |
|---|---|---|
| `tests/test_policy_rbi.py` | **Written first.** The compliance evidence | §5 |
| `tests/test_policy_declines.py` | **Written first.** Hard/action retry bans | §5 |
| `tests/test_idempotency.py` | The double-charge guard | §5 |
| `recovery/policy/engine.py` | `evaluate()` → `Decision`, runs all rules | §5 |
| `recovery/policy/rules.py` | The nine operational rules | §5 |
| `recovery/policy/rbi.py` | The three RBI rules, with inline citations | §5 |
| `recovery/scoring.py` | P(recover \| intervention), expected value | §8 |
| `recovery/arms/baseline.py` | Razorpay's T+3 default | §9 day 2 |

---

## Specification

### `recovery/policy/engine.py`

Every rule returns `Verdict(allow | deny, rule_id, reason, evidence)`. Every money or
contact action routes through:

```python
PolicyEngine.evaluate(action, case, ledger, clock) -> Decision
```

**The engine runs all rules and does not short-circuit.** Returning as soon as one
rule denies would be faster and would destroy the thing that makes this project's
audit trail interesting: the ledger records the *full* verdict set, every rule, pass
and fail. `Decision.allowed` is the AND of every verdict; `Decision.verdicts` is the
complete list.

`evidence` on each verdict carries the values the rule actually looked at — the
attempt count it compared against the cap, the timestamp it compared against the
notice window. A verdict that says "denied" without saying what it saw is not audit
evidence, it is an assertion.

### `recovery/policy/rules.py` — the nine operational rules

| Rule | Constraint | Why |
|---|---|---|
| `HardDeclineNoRetry` | Never retry a HARD-class decline | Burns fees and trips issuer fraud flags, degrading approval rate on good transactions |
| `ActionDeclineNoRetry` | Never silently retry an ACTION-class decline | Cannot succeed by construction; contact instead |
| `AttemptCap` | Stop when marginal expected recovery < marginal cost | Derived from economics, not a magic number — and we can say why |
| `CoolingOff` | Enforce `min_retry_wait_h`; payroll-aligned 24–72h for `insufficient_funds` | The largest decline bucket; retrying an empty account sooner is pure waste |
| `ContactFrequency` | Max contacts per customer per rolling window | Fatigue is a real cost, not a free action |
| `QuietHours` | No SMS/WhatsApp 21:00–09:00 IST | TRAI-aligned; dunning to Indian consumers is its own regulated surface |
| `BudgetCeiling` | Cumulative spend on a case never exceeds its expected recovery | Recovery that costs more than it recovers is a loss dressed as a win |
| `Idempotency` | The same `(case, action, key)` can never execute twice | The bug that double-charges a customer |
| `TerminalState` | No action on a recovered, closed, or refunded case | The bug that dunts someone who already paid |

**`AttemptCap` must be derived, not hard-coded.** This is the one rule where a
reviewer will ask "why three?" The answer must be that it is not three — it is
"stop when `P(success | attempt n) × amount < cost_of_attempt`", computed from
`recovery/scoring.py`, which happens to bottom out around the third attempt for
typical amounts. A literal `MAX_ATTEMPTS = 3` throws away the best answer in the
rule set.

`Idempotency` and `TerminalState` read the ledger — they are the two rules whose
state lives in history rather than in the case. That is why `evaluate()` takes the
ledger as a parameter.

### `recovery/policy/rbi.py` — the three RBI rules

**Cite the source inline, at the rule**, not only in the module docstring. A reviewer
skimming the file should hit the citation next to the constant it justifies.

| Rule | Constraint |
|---|---|
| `RBIPreDebitNotice` | An e-mandate debit requires notification **≥24h prior**, carrying merchant name, amount, date/time, and mandate reference |
| `RBIAdditionalFactorAuth` | AFA required above **₹15,000** (**₹1,00,000** for insurance, mutual funds, and credit-card bills), **and mandatorily on the first debit under a mandate regardless of amount** |
| `RBIPostDebitNotice` | Post-transaction notification must include grievance-redressal details |

#### Provenance

The RBI Digital Payments — E-mandate Framework was **notified 21 April 2026** and
took effect immediately. It consolidates prior e-mandate circulars and makes
**acquirers responsible for their merchants' compliance** — which is to say, this is
Razorpay's problem, not just the merchant's. That fact is worth one sentence in
`ARCHITECTURE.md`: it explains why a payments processor cares about a rule that
nominally binds its customers.

These figures were verified against live sources during planning. **The first-debit
AFA requirement regardless of amount is a rule the research brief missed** — encode
it explicitly and test it explicitly, because it is the detail that shows the
regulation was actually read rather than summarised.

Thresholds in paise: ₹15,000 = `1_500_000`; ₹1,00,000 = `10_000_000`. Never floats.

#### Keep compliance as seasoning

A panel of engineers will be impressed that you knew the regulation exists and
encoded it. They will glaze over if the pitch becomes a compliance lecture. Budget
roughly **30 seconds** of the five-minute video: show one blocked action, name the
rule, move on. The measured recovery number stays the headline. Build the rules
thoroughly; talk about them briefly.

### `recovery/scoring.py`

`P(recover | intervention)` and expected value, used by `AttemptCap` and
`BudgetCeiling`.

**This module must not read latents.** It is the system's *estimate* of recovery
probability, built from observable features — decline reason class, attempt number,
hours since failure, amount, contacts already sent. The simulator knows the truth;
the scorer guesses. Those being different is what makes the evaluation honest, and a
scorer that peeks at `Case.latents` would produce an agent with oracle powers and a
result that means nothing.

A deliberately imperfect scorer is also good material for `WHAT_BROKE.md` — a
confidently-wrong scorer is one of the three incidents the submission checklist
explicitly suggests.

### `recovery/arms/baseline.py`

Razorpay's T+3 default: retry once a day for three days, no contact, no
reason-awareness.

Route it through the policy gate like every other arm. Two consequences worth
noticing:

- The baseline will get **denied** on HARD and ACTION declines, because
  `HardDeclineNoRetry` and `ActionDeclineNoRetry` block it. That is realistic —
  it is exactly the naive behaviour the rules exist to prevent — and the denials
  populate the blocked-action log with real entries before the agent arm exists.
- If you would rather show the baseline's *unconstrained* naive behaviour, add an
  `--ungated-baseline` flag rather than removing the gate. The comparison
  "what the naive policy would have done vs what the gate allowed" is a good 15
  seconds of video.

---

## Invariants this phase must not violate

- **Every money or contact action routes through `PolicyEngine.evaluate()`.** No
  bypass in a test helper, no bypass in a debug path.
- **The engine does not short-circuit.** All rules run; all verdicts are logged.
- **Every evaluation is written to the ledger, allow and deny.** The denials are the
  blocked-action log.
- **`scoring.py` never reads latents.**
- **All thresholds are integer paise.**
- **Unknown decline codes still raise** — the policy engine must not catch
  `UnknownDeclineReason` and treat it as a soft default.

---

## Tests

### `tests/test_policy_rbi.py` — write this file first

These tests are the compliance evidence. Name them so a reviewer reading only the
test names learns the regulation.

- An e-mandate debit with notice sent 23h59m prior is **denied**; 24h01m prior is
  allowed. Test the boundary from both sides.
- A pre-debit notice missing any of merchant name, amount, date/time, or mandate
  reference is denied — one test per missing field.
- A ₹15,001 standard-category debit without AFA is denied; ₹14,999 is allowed.
- A ₹99,999 insurance / mutual-fund / credit-card-bill debit without AFA is allowed;
  ₹1,00,001 is denied. One test per elevated category.
- **A ₹1 first debit under a mandate without AFA is denied.** This is the
  brief-missed rule; give it an unmissable test name such as
  `test_afa_required_on_first_debit_regardless_of_amount`.
- A post-debit notification without grievance-redressal details is denied.
- Clock-skew case: an action evaluated exactly at the 24h boundary behaves
  deterministically and does not depend on wall-clock time. Phase 7 attacks this
  boundary adversarially; the test here is the first line of defence.

### `tests/test_policy_declines.py` — write this file first

- Every HARD reason denies a retry. Parametrise over the whole taxonomy so a reason
  added later without a class is caught.
- Every ACTION reason denies a retry but **allows** a contact.
- SOFT reasons allow a retry after `min_retry_wait_h` and deny before it.
- `insufficient_funds` respects the payroll-aligned 24–72h window.
- `QuietHours` denies SMS and WhatsApp at 22:00 and 08:00 IST, allows at 10:00, and
  **allows email at every hour** — email is not covered by the TRAI-aligned window.
- `BudgetCeiling` denies the action that would push cumulative case spend past
  expected recovery, and allows the one just below it.
- `AttemptCap` denies based on the computed marginal economics, not a literal
  constant. Assert that changing the case amount changes the cap.

### `tests/test_idempotency.py`

- The same `(case, action, idempotency_key)` executes once; the second call is denied
  and the ledger shows exactly one `action` entry and two `policy_check` entries.
- A different key for the same logical action still executes — the key is the
  identity, and this is what makes retry-after-timeout safe.
- Concurrent-ish replay: evaluate the same key twice without an intervening write and
  confirm the second is denied.

### Engine-level tests

- `Decision.verdicts` contains one entry per registered rule, **even when the first
  rule denies.** This is the anti-short-circuit test.
- A denied action writes a ledger entry with `allowed=False` and a populated
  `rule_id`.
- `TerminalState` denies every action type on a recovered case.

---

## Exit criteria

- [ ] `pytest tests/test_policy_rbi.py tests/test_policy_declines.py tests/test_idempotency.py -v`
      passes, and the RBI test names read as a description of the regulation
- [ ] `python -m recovery.cli eval --seed 42 --n 500 --arms control,baseline` produces
      **a genuine incremental number for baseline over control**
- [ ] The blocked-action log has real entries:
      `grep -c '"allowed": false' data/results/<run_id>/baseline.jsonl` is > 0
- [ ] Blocked actions grouped by `rule_id` appear in the results table
- [ ] The ledger still verifies after a full baseline run
- [ ] `grep -rn "MAX_ATTEMPTS\s*=\s*3" recovery/` returns nothing

**The baseline number will be unimpressive.** That is the floor, and having it on the
board before the agent exists is the point — it means the submission has a real
measured result even if phase 5 never lands.

---

## Pitfalls

- **Short-circuiting on first denial.** It is the natural way to write the loop and it
  quietly halves the value of the audit trail. Run every rule.
- **A hard-coded attempt cap.** See `AttemptCap` above. This is the rule most likely
  to be asked about on video.
- **Letting the scorer see latents.** The import is right there in `cohort.py` and it
  makes every number better and meaningless.
- **Float thresholds.** `15000.0` rupees instead of `1_500_000` paise. The comparison
  will *usually* be right, which is worse than always being wrong.
- **Testing the AFA amount rule and forgetting the first-debit rule.** The amount
  thresholds are the memorable part; the first-debit requirement is the part that
  demonstrates the regulation was read. Both need tests.
- **Writing the rules before the tests.** The tests are the deliverable here as much
  as the rules are. Written afterwards, they tend to assert what the code does rather
  than what the regulation requires.
- **Turning the video into a compliance lecture.** Thorough rules, 30 seconds of
  airtime.
- **Do not build the agent here.** The baseline arm is a fixed policy, not a model. No
  `anthropic` import belongs in this phase.
