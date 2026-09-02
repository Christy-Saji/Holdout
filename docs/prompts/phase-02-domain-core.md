# Phase 2 — Domain core

> Track 03 · Razorpay AI Buildathon · rebased target **Sat 30 August** (with phases 1 and 3)
> Master spec: `docs/prompts/track-03-build-plan.md` §3.1, §4.1, §4.2, §4.3
> Previous phase: `phase-01-scaffold-and-tooling.md` · Next: `phase-03-simulation-and-harness.md`

---

## Prompt

> You are building the Razorpay AI Buildathon Track 03 submission — an AI revenue
> recovery system whose headline deliverable is **incremental** money recovered
> against a holdout control arm.
>
> This phase builds the domain core: the decline taxonomy, the synthetic cohort
> generator with hidden latent ground truth, and the append-only hash-chained audit
> ledger. No simulation loop, no policy rules, no agent — those are phases 3, 4 and 5.
>
> Three things in this phase are load-bearing and easy to get subtly wrong: unknown
> decline codes must **raise**, all money is **integer paise**, and every random draw
> comes from the **blake2b helper** rather than a sequential RNG. Read the
> specification below carefully on those three points.
>
> Read `CLAUDE.md` for the project invariants. Everything else you need is below.
>
> **Standing rule for this project: show your plan in your reply as visible text and
> wait for explicit approval before creating or editing any implementation file.**

---

## Prerequisites

- Phase 1 complete: `python -m recovery.cli --help` prints four subcommands
- `python -c "import recovery"` succeeds
- `numpy` importable (2.4.6 is installed)
- No credentials required

---

## Deliverables

| File | Contents | Spec |
|---|---|---|
| `recovery/declines.py` | Three-class decline taxonomy | §4.1 |
| `recovery/cohort.py` | `Case`/`CustomerProfile`/`Latents`, generator, CRN helper | §3.1, §3.2, §4.2 |
| `recovery/ledger.py` | `LedgerEntry`, append-only hash-chained JSONL writer | §4.3 |
| `tests/test_cohort.py` | Determinism and CRN properties | §10 |
| `tests/test_ledger.py` | Chain verification and tamper detection | §10 |
| `data/cohort_seed42.jsonl` | Generated artifact, committed | §4.2 |

---

## Specification

### `recovery/declines.py`

**Three classes, not two.** This is the distinction that proves the system was built
by someone who has read a decline report.

| Class | Meaning | Correct response |
|---|---|---|
| `SOFT` | Transient — no funds, issuer down, timeout | Retry, correctly timed |
| `HARD` | Permanent — stolen card, bad account number, invalid VPA | **Never retry.** Retrying burns a fee *and* trips issuer fraud heuristics, degrading the approval rate on your good transactions |
| `ACTION` | Instrument needs the customer to change something — expired card, revoked mandate, failed 3DS | Silent retry can never succeed. Contact is the only move |

The `ACTION` class refines the research brief's binary hard/soft split, and it
matters: it is a large slice of real recurring-payment failures where the naive
system retries forever and the merely-competent system gives up. The right answer is
neither.

Each reason carries:

| Field | Type | Notes |
|---|---|---|
| `code` | `str` | The reason string |
| `klass` | `DeclineClass` | SOFT / HARD / ACTION enum |
| `min_retry_wait_h` | `int` | Cooling-off hours before a retry can succeed at all |
| `retry_success_base` | `float` | Base P(success) for a well-timed first retry |
| `self_heal_rate` | `float` | P(recovers with no intervention inside the 336h window) |
| `population_weight` | `float` | Share of the generated cohort |
| `methods` | `frozenset[Method]` | Payment methods this reason can occur on |

**`retry_success_base = 0.0` for every HARD and ACTION reason.** No amount of
retrying fixes a stolen card.

**ACTION reasons have `retry_success_base = 0.0` but non-zero `self_heal_rate`.**
Customers do notice a dead subscription and update the card unprompted. That non-zero
self-heal is exactly what inflates naive gross-recovery numbers, so modelling it is
load-bearing rather than decorative — it is the mechanism by which a control arm
recovers money without anyone doing anything.

Cover at least: `insufficient_funds`, `issuer_unavailable`, `gateway_timeout`,
`do_not_honour`, `risk_declined` (SOFT); `stolen_card`, `lost_card`,
`invalid_account`, `invalid_vpa`, `account_closed` (HARD); `expired_card`,
`mandate_revoked`, `mandate_paused`, `authentication_failed`, `card_not_enrolled`
(ACTION). Methods: `card`, `upi`, `netbanking`, `emandate`, `wallet`.

**Population weights must sum to 1.0** — assert it at import time.

#### The provenance docstring — required, not optional

The module docstring must state, in substance:

> Reason codes are modelled on Razorpay's `error.reason` field combined with
> card-network decline categories. Razorpay documents reason codes per payment method
> and does not publish a single exhaustive enumeration, so **this is a documented
> approximation, not a transcription.** Population weights and success rates are our
> own priors, stated openly and swept in the sensitivity analysis (phase 6). The one
> external anchor: insufficient funds is consistently reported as roughly **44%** of
> card-not-present issuer declines.

Do not present the taxonomy as authoritative. An attempt was already made to pull an
exhaustive list from `razorpay.com/docs/errors/` and it yielded only the error
*structure* (`code`, `description`, `source`, `step`, `reason`, `metadata`) plus two
examples. Do not repeat that attempt.

#### Unknown codes raise

```python
def lookup(code: str) -> DeclineReason:
    """Raise UnknownDeclineReason on an unrecognised code. Never default to SOFT."""
```

Silently defaulting an unrecognised reason to soft is exactly how a dunning system
starts retrying stolen cards. Define a module-level `UnknownDeclineReason(KeyError)`
and raise it.

### `recovery/cohort.py`

#### The CRN helper — reproduce exactly

```python
def u(seed: int, case_id: str, event_kind: str, sequence: int) -> float:
    """Uniform [0,1) draw keyed by event identity, not by stream position."""
    h = hashlib.blake2b(f"{seed}|{case_id}|{event_kind}|{sequence}".encode())
    return int.from_bytes(h.digest()[:8], "big") / 2**64
```

This is **common random numbers**, a standard variance-reduction technique. Its
effect: the hour at which case #237 would self-heal is *identical* in the control arm
and the agent arm, so the difference between arms is attributable purely to the
intervention rather than to sampling noise. That dramatically tightens the confidence
interval on incremental lift for the same cohort size, and it means arms can run in
any order, in parallel, or in separate processes with the comparison staying valid.

Every stochastic draw in this project — cohort generation, self-heal, retry outcome,
contact response — goes through this function. Never `random.random()`, never a
sequential `numpy.Generator.random()` call whose result depends on how many draws
preceded it.

#### Dataclasses — all frozen

```python
@dataclass(frozen=True)
class Latents:
    p_self_heal: float          # P(recovers inside the window with no intervention)
    retry_success_base: float   # P(a well-timed first retry succeeds)
    intent_to_pay: float        # per-customer willingness multiplier
    responsiveness: float       # per-customer contact-effectiveness multiplier
    channel_affinity: dict      # per-channel multiplier: sms / whatsapp / email

@dataclass(frozen=True)
class CustomerProfile:
    customer_id: str
    segment: str                # new | regular | loyal

@dataclass(frozen=True)
class Case:
    case_id: str
    customer: CustomerProfile
    payment_id: str
    amount_paise: int           # integer paise everywhere; never float rupees
    method: Method              # card | upi | netbanking | emandate | wallet
    decline_reason: str
    failed_at: datetime         # IST
    is_recurring: bool
    mandate_first_debit: bool   # AFA required regardless of amount (phase 4)
    mandate_category: str       # standard | insurance | mutual_fund | credit_card_bill
    latents: Latents            # HIDDEN from every arm
```

**Latents are hidden.** They are drawn at generation time and read **only** by the
simulator in phase 3. No arm, no scorer, and no policy rule may read
`Case.latents`. Because the simulator owns them, the counterfactual is exact rather
than estimated — we are not inferring what would have happened, we know, because we
specified it. Any code path that reads latents outside the simulator invalidates the
entire result.

Consider enforcing this structurally: serialise latents to a separate side-table
keyed by `case_id` that only the transport layer loads, rather than trusting
convention. A reviewer noticing that the agent *cannot* see the answer is worth more
than a comment saying it does not look.

#### Generation

- **Defaults:** `n = 500` (configurable via `--n`), `seed = 42`.
- **Method mix:** UPI-heavy, matching Indian payment reality.
- **Amounts:** log-normal with mass in the ₹200–₹5,000 range, plus a distinct
  subscription cluster. Store as integer paise.
- **Segments:** new / regular / loyal, driving `intent_to_pay` and `responsiveness`.
- **Decline reasons:** sampled by `population_weight`, filtered to reasons valid for
  the drawn method.
- **`p_self_heal`** derives from the reason's `self_heal_rate`, modulated by
  `intent_to_pay`.
- **Output:** `data/cohort_seed42.jsonl`, one JSON object per line, committed to the
  repo so a reviewer gets byte-identical input.

Add a CLI hook so the file can be regenerated: extend `recovery/cli.py` with a
`cohort` subcommand, or accept regeneration as a side effect of `eval`.

### `recovery/ledger.py`

Append-only JSONL. One entry per policy evaluation, action, and outcome.

```python
@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    prev_hash: str            # hash chain — tamper-evidence
    entry_hash: str
    ts: datetime              # simulated clock, not wall clock
    run_id: str
    arm: str
    case_id: str
    kind: str                 # policy_check | action | outcome | observation
    action: dict | None
    verdicts: list            # EVERY rule verdict, pass and fail
    allowed: bool | None
    idempotency_key: str | None
    cost_paise: int
    result: dict | None
```

Two design points, both worth defending on video:

- **Every evaluation is logged, allow and deny.** The denials are the blocked-action
  log. An audit trail that only records what happened is half an audit trail; what
  the system *refused to do* is the interesting half.
- **Hash chain.** Each entry carries the hash of its predecessor. Cheap to implement,
  and it makes "append-only" a structural property rather than a claim. This is the
  kind of detail that reads as payments-adjacent thinking.

Implementation notes:

- `entry_hash = sha256(canonical_json(entry_without_entry_hash) + prev_hash)`.
  Canonicalise with `json.dumps(..., sort_keys=True, separators=(",", ":"))` so the
  hash is stable across Python versions and dict insertion orders.
- Genesis entry uses `prev_hash = "0" * 64`.
- Expose `verify(path) -> bool` that walks the file and recomputes the chain.
- `ts` is the **simulated** clock passed in by the harness, never `datetime.now()`.
  Wall-clock timestamps would make runs non-reproducible.
- The writer opens in append mode and flushes per entry. There is no update path and
  no delete path — do not add one.

---

## Invariants this phase must not violate

- **All money is integer paise. Never float rupees.** Floating-point money in a
  payments system is a correctness bug waiting to happen and an instant credibility
  loss with this audience.
- **Unknown decline reason codes raise.** Never default to `SOFT`.
- **Every draw goes through `u()`.** No `random.random()`, no sequential RNG stream.
- **Latents are hidden** from arms, scorer and policy engine.
- **The ledger is append-only.** No update path, no delete path.
- **`ts` is the simulated clock.** Never wall-clock time.

---

## Tests

### `tests/test_cohort.py`

- Same seed produces a **byte-identical** cohort. Generate twice, serialise both,
  compare the bytes — not a field-by-field comparison, which can miss ordering drift.
- `u()` is stable: a known `(seed, case_id, event_kind, sequence)` tuple returns a
  hard-coded expected float. This catches an accidental change to the hash input
  format, which would silently invalidate every published number.
- `u()` is order-independent: drawing for case B before case A gives the same value
  for case A as drawing A first.
- All `amount_paise` values are `int`, never `float`.
- Population weights sum to 1.0 within tolerance.
- Every generated `decline_reason` is valid for its `method`.

### `tests/test_ledger.py`

- A freshly written chain verifies.
- Mutating any field of any entry in the file causes `verify()` to return `False`.
- Mutating an `entry_hash` to match its own tampered body still fails, because the
  *next* entry's `prev_hash` no longer matches.
- Genesis `prev_hash` is 64 zeros.

### `tests/test_declines.py`

- `lookup("not_a_real_code")` raises `UnknownDeclineReason` — explicitly assert it
  does **not** return a SOFT default.
- Every HARD and ACTION reason has `retry_success_base == 0.0`.
- At least one ACTION reason has `self_heal_rate > 0.0`.

---

## Exit criteria

- [ ] `pytest tests/test_cohort.py tests/test_ledger.py tests/test_declines.py` passes
- [ ] `python -m recovery.cli eval --seed 42 --n 500` generates
      `data/cohort_seed42.jsonl` with 500 lines
- [ ] Running the generator twice produces byte-identical output:
      `python -m recovery.cli eval --seed 42 --n 500 && sha256sum data/cohort_seed42.jsonl`
      gives the same digest both times
- [ ] `python -c "from recovery.declines import lookup; lookup('bogus')"` raises
- [ ] `grep -c "float" data/cohort_seed42.jsonl` finds no decimal point in any
      `amount_paise` value
- [ ] The `declines.py` module docstring contains the words "approximation" and "priors"

---

## Pitfalls

- **The seductive default.** Writing `reasons.get(code, SOFT_DEFAULT)` feels
  defensive and is the single most dangerous line you could write in this file. Raise.
- **Sequential RNG creep.** It is very easy to reach for `rng.random()` inside the
  generation loop "just for this one draw." That silently breaks the pairing that the
  entire measurement design rests on. Every draw goes through `u()`.
- **Float rupees creep in at the boundary.** `int(amount_rupees * 100)` is a bug —
  `int(2.675 * 100) == 267`. Generate in paise directly, or round explicitly.
- **`datetime.now()` in the ledger.** Makes runs non-reproducible and will fail the
  phase 8 clean-clone reproduction gate. Pass the simulated clock in.
- **Non-canonical JSON in the hash.** Default `json.dumps` preserves insertion order
  and adds spaces; two logically identical entries can hash differently. Sort keys and
  strip separators.
- **Do not implement the outcome model here.** The self-heal hazard, retry-success
  product and contact-response model belong in phase 3's `transport/mock.py`. This
  phase only *stores* the latents that model will consume.
