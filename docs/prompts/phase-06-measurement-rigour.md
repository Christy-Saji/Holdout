# Phase 6 — Measurement rigour

> Track 03 · Razorpay AI Buildathon · rebased target **Tue 2 September**
> Master spec: `docs/prompts/track-03-build-plan.md` §3.4, §7
> Previous phase: `phase-05-agent-layer.md` · Next: `phase-07-live-integration-and-adversarial.md`

---

## Prompt

> You are building the Razorpay AI Buildathon Track 03 submission — an AI revenue
> recovery system whose headline deliverable is **incremental** money recovered
> against a holdout control arm.
>
> This phase is the intellectual centre of the submission. Everything before it was
> engineering; this is the part that makes the number credible. You are building
> paired bootstrap confidence intervals, a sensitivity sweep over the generative
> model's own assumptions, the time-to-cash metric, and the report that produces the
> table and figures for the README and the video.
>
> The governing commitment: **if the agent only narrowly beats the baseline, report
> that. If the sign of the lift does not survive the sensitivity sweep, say so in the
> README.** An honest small number is worth more than an inflated large one, and this
> is the trait the whole competition is screening for.
>
> Read `CLAUDE.md` for the project invariants. Everything else you need is below.
>
> **Standing rule for this project: show your plan in your reply as visible text and
> wait for explicit approval before creating or editing any implementation file.**

---

## Prerequisites

- Phase 5 complete, or phase 5 skipped for lack of a key — this phase works with
  whatever arms exist, and `control,baseline` alone is a legitimate two-arm analysis
- `data/results/<run_id>/*.jsonl` populated with per-case outcomes
- `.venv/` activated, then `python -m pip install matplotlib` — **into the venv, never
  globally.** Confirm with `python -c "import sys; print(sys.prefix)"`
- `numpy` importable **from inside the venv** — a global 2.4.6 does not count
- No credentials required

---

## Deliverables

| File | Contents | Spec |
|---|---|---|
| `recovery/eval/metrics.py` (extend) | Paired bootstrap, time-to-cash, contact fatigue | §7 |
| `recovery/eval/report.py` | Markdown results table + figure generation | §8 |
| `scripts/make_figures.py` | Matplotlib figures for README and video | §8 |
| `recovery/cli.py` (extend) | Wire up `report` and `sweep` | §8 |
| `tests/test_metrics.py` (extend) | Bootstrap and sweep correctness | §10 |
| `data/results/` | Committed run outputs and figures | §8 |

---

## Specification

### The objection this phase exists to answer

A reviewer's first and most obvious objection is: *you invented your own success
probabilities, so of course your agent wins.* **That objection is correct and must be
met head-on rather than buried.** The README says so on its first screen: this is a
simulation result under a published generative model, not live-money evidence.

Three things convert that concession into a strength, and this phase builds two of
them:

1. **The generative model is published in full** — every parameter in `declines.py`
   and `cohort.py` is visible, commented with its provenance, and marked where it is
   our own prior rather than an external anchor. (Done in phase 2.)
2. **Bootstrap 95% confidence intervals** on incremental lift, using a *paired*
   bootstrap.
3. **A sensitivity sweep** varying `p_self_heal` by ±50% and the attempt-decay curve
   across plausible ranges, reporting whether the **sign** of the lift survives.

### Paired bootstrap

```
for b in 1..B:                     # B = 2000
    idx = resample case indices with replacement
    recompute EVERY arm on that same idx
    delta_b = R_agent(idx) − R_control(idx)
report the 2.5th and 97.5th percentiles of delta
```

**Resample case indices, not arms.** Because the arms share a cohort and share
latents through common random numbers, the pairing is real information. Resampling
each arm independently throws away the pairing that CRN bought and inflates the
interval — the result would still be publishable, just needlessly wide, and it would
undo the main methodological choice in the project.

Percentile method, B = 2000. Seed the bootstrap resampling itself so the reported
interval is reproducible; a CI that moves between runs cannot be published as a
number.

### Full metric set

Let `R_a` be gross paise recovered in arm `a`, `N` the cohort size, `C_a` the cost.

| Metric | Formula | Note |
|---|---|---|
| Gross recovered | `R_a = Σ amount_i · 1[recovered_i]` | The number everyone else reports |
| **Incremental recovery** | `Δ_a = R_a − R_control` | **The headline** |
| Recovery rate | `n_recovered_a / N` | |
| Incremental rate | `(n_recovered_a − n_recovered_control) / N` | |
| **Cost per incremental ₹** | `C_a / Δ_a` | Denominator is *incremental*, not gross |
| Contact fatigue | mean and P95 contacts per customer | |
| Blocked actions | count by `rule_id` | Straight from the ledger |
| **Time-to-cash** | mean days from failure to recovery | See below |
| Bootstrap CI | paired, B = 2000, percentile method | |

**Time-to-cash earns its place.** It has real value even when both arms recover the
same case: the same rupee arriving nine days earlier is worth something in working
capital. It is also the metric most likely to show the agent winning when gross
recovery barely moves, so it is worth computing even if the headline is flat — and
saying "we recovered the same money eleven days sooner" is a good result honestly
stated.

**Cost per incremental rupee** keeps its incremental denominator. Using gross flatters
the result and is the same error as reporting gross recovery in the first place. When
`Δ_a <= 0` the ratio is undefined — report it as undefined, not as infinity and not
by quietly switching to a gross denominator.

### Sensitivity sweep

```bash
python -m recovery.cli sweep --param p_self_heal --range 0.5,1.5
```

Scale the swept parameter across the range, re-run all arms, and report the
incremental lift at each point. Sweep at minimum:

- `p_self_heal` × [0.5 … 1.5] — the parameter the whole critique rests on
- the attempt-decay curve, across plausible alternative shapes
- the cost assumptions (retry ₹3, SMS ₹0.20, WhatsApp ₹0.35, email ₹0.01) — these are
  **our own priors, not quoted rates**, and cost per incremental rupee moves directly
  with them

**The question the sweep answers is whether the *sign* of the lift is stable**, not
whether its magnitude is. Magnitude under a synthetic model is not evidence; a sign
that holds across a 3× swing in the most contested parameter is.

If the sign is **not** stable, that goes in the README as a stated limitation rather
than being quietly dropped. That outcome is a finding, not a failure — and reporting
it is worth more to this particular audience than a clean result would be.

Sweeps re-run the arms. With the agent arm that means cassette replay, which is free
and offline — but a swept parameter changes case state and can therefore change what
the agent decides, producing cassette misses. Either record cassettes for each sweep
point once, or restrict the sweep to control and baseline and say so in the README.
**Do not let a sweep silently fall through to live API calls.**

### `recovery/eval/report.py` and figures

Produces the markdown table that goes in the README verbatim, plus the figures.

Figure set, kept small — every figure must earn its place on a five-minute video:

1. **Incremental recovery by arm, with CI whiskers.** The headline. Control at zero
   as the reference line.
2. **Sensitivity: sign stability of the lift across the `p_self_heal` sweep.** The
   credibility figure.
3. **Blocked actions by `rule_id`.** The compliance figure — 30 seconds of airtime.
4. Optional: cumulative recovery over the 336-hour window by arm, which shows
   time-to-cash visually.

Rules for the figures, because they go on screen:

- **Put the number on screen as a number**, with the control arm beside it. Do not
  narrate a figure over a scrolling terminal.
- Label axes in rupees, not paise — convert at the presentation boundary only.
- Readable at video resolution: large fonts, few gridlines, no default matplotlib
  small text.
- Deterministic output. Set a fixed figure size and DPI so regenerating does not
  produce a diff.

Write figures to `data/results/` and commit them.

---

## Invariants this phase must not violate

- **Paired bootstrap.** Resample case indices; recompute all arms on the same
  resample.
- **Cost per incremental rupee uses the incremental denominator.**
- **All money is integer paise internally.** Convert to rupees only for display.
- **Sweeps never silently make live API calls.**
- **Report what you find.** A flat or negative result is published, with the sweep
  that produced it.

---

## Tests

Extend `tests/test_metrics.py`:

- Bootstrap on a fixture where the true difference is exactly zero produces a CI that
  **contains** zero.
- Bootstrap on a fixture with a large, consistent difference produces a CI that
  **excludes** zero.
- **The pairing test:** a paired bootstrap on the same fixture yields a strictly
  narrower interval than an unpaired one. This is the test that proves the CRN design
  is actually paying off; if the intervals match, the pairing is not being used.
- The bootstrap is reproducible: same seed, same interval, to the last digit.
- Time-to-cash is `None`, not zero, for a case that never recovered — a non-recovery
  scored as "recovered on day 0" would be a silent, large bias.
- Contact fatigue P95 on a hand-built fixture matches the hand-computed value.
- Sweep at scale factor 1.0 reproduces the unswept baseline result exactly.

---

## Exit criteria

- [ ] `python -m recovery.cli report` produces the markdown table and the figures that
      go in the README and on screen in the video
- [ ] The results table shows, per arm: gross, incremental, incremental CI, recovery
      rate, cost per incremental ₹, mean time-to-cash, and blocked actions by rule
- [ ] `python -m recovery.cli sweep --param p_self_heal --range 0.5,1.5` runs and
      reports lift at each sweep point
- [ ] The sign-stability verdict is stated explicitly — "the sign of the lift holds
      across `p_self_heal` ±50%" or "it does not, and here is where it flips"
- [ ] `pytest tests/test_metrics.py` passes, **including the paired-vs-unpaired
      interval-width test**
- [ ] Figures are committed under `data/results/` and are legible at video resolution
- [ ] Re-running `report` produces byte-identical figures

---

## Pitfalls

- **The unpaired bootstrap.** It is the version everyone writes from memory, it runs
  without error, and it silently discards the project's main methodological
  advantage. The interval-width test exists to catch exactly this.
- **An unseeded bootstrap.** The published CI would move between runs and could not be
  quoted as a number in the README.
- **Time-to-cash counting non-recoveries as zero.** Large silent bias toward whichever
  arm recovers less.
- **Rupee/paise confusion at the display boundary.** Convert once, at the edge, and
  label the axis.
- **A sweep that triggers live API calls.** A cassette miss under a swept parameter is
  easy to produce and expensive to discover afterwards. Decide the policy explicitly
  and state it in the README.
- **Too many figures.** Four at most; three is better. Every figure that does not go
  on screen is time not spent on the ones that do.
- **Burying an unfavourable sweep result.** This is the single most costly thing you
  could do in this project. The competition is explicitly screening for the opposite
  behaviour, and a limitation stated plainly reads as confidence.
- **Do not start writing the README here.** Phase 8 owns the judged artifacts. This
  phase produces the table and figures they will contain.
