# Phase 8 — Judged artifacts and submission

> Track 03 · Razorpay AI Buildathon · rebased target **Wed 3 – Thu 4 September**
> Master spec: `docs/prompts/track-03-build-plan.md` §9 days 6–7, §10, §12
> Previous phase: `phase-07-live-integration-and-adversarial.md` · Next: none

---

## Prompt

> You are finishing the Razorpay AI Buildathon Track 03 submission — an AI revenue
> recovery system whose headline deliverable is **incremental** money recovered
> against a holdout control arm.
>
> Every phase before this built the thing. This phase produces the four artifacts that
> are actually judged: the public repository, the architecture write-up, the "what
> broke" answer, and the five-minute video. Then it submits.
>
> The repository is not the deliverable — **the repository plus a stranger's ability to
> reproduce it** is. The gate for this phase is a clean clone into a temp directory
> that installs, runs, and produces the numbers the README claims, byte for byte.
>
> Read `CLAUDE.md` for the project invariants. Everything else you need is below.
>
> **Standing rule for this project: show your plan in your reply as visible text and
> wait for explicit approval before creating or editing any implementation file.**

---

## Prerequisites

- Phase 6 complete: results table and figures exist in `data/results/`
- Phase 7 complete **or deliberately skipped** — either is fine
- `pytest` green
- Git history clean, no secrets committed

---

## Deliverables

| File | Contents | Spec |
|---|---|---|
| `README.md` | Results table and honest caveat above the fold | §12 |
| `ARCHITECTURE.md` | Seven layers, both departures, with reasoning | §12 |
| `WHAT_BROKE.md` | Finalised from incidents recorded as they happened | §12 |
| `recovery/cli.py` (extend) | `repro` subcommand — the clean-clone gate | §10 |
| The video | Five minutes, structured below | §9 day 7 |

---

## Specification

### `README.md`

**Results table and honest caveats above the fold.** A reviewer who reads only the
first screen must come away with the number, the control comparison, and the caveat.
Not the install instructions, and not a feature list.

First screen, in order:

1. One sentence on what this is.
2. **The results table** — per arm: gross recovered, incremental recovered,
   incremental 95% CI, recovery rate, cost per incremental ₹, mean time-to-cash,
   blocked actions.
3. **The caveat, stated plainly:** this is a simulation result under a published
   generative model, not live-money evidence. The generative model is published in
   full; every parameter is visible and marked where it is our own prior rather than
   an external anchor.
4. **The sensitivity verdict** — whether the sign of the lift survives `p_self_heal`
   ±50%. If it does not, say so here, not in an appendix.
5. Cost assumptions named as priors: retry ≈ ₹3, SMS ≈ ₹0.20, WhatsApp ≈ ₹0.35,
   email ≈ ₹0.01. **These are our own priors, not quoted rates.**

Then, below the fold: install, run, reproduce, repo map, limitations.

The reproduction instructions must be exactly what the clean-clone gate runs. If they
differ by one flag, they are wrong.

### `ARCHITECTURE.md`

The seven layers, and — the part that distinguishes it — **the two departures from the
obvious approach, with reasoning.**

The layers:

| L | Layer | Module |
|---|---|---|
| L1 | Cohort generation and latent ground truth | `cohort.py`, `declines.py` |
| L2 | Scoring — P(recover \| intervention), expected value | `scoring.py` |
| L3 | Policy engine and rules, including RBI | `policy/` |
| L4 | Agent — gated tools over the SDK tool runner | `agent/` |
| L5 | Transport — deterministic mock, optional live test mode | `transport/` |
| L6 | Audit ledger — append-only, hash-chained | `ledger.py` |
| L7 | Evaluation harness and metrics | `eval/` |

The two departures, each with its reasoning stated rather than implied:

1. **Anthropic SDK tool runner, not the Claude Agent SDK.** The Agent SDK is Claude
   Code packaged as a library — built-in file and bash tools, a coding harness. Using
   it to decide whether to send an SMS at 14:00 would be the wrong tool. The tool
   runner drives the loop over tools we define, and Anthropic's own guidance puts
   approval gating *inside the tool function* — which is precisely the policy seam
   this system needs, and which produces the blocked-action log for free.
2. **Mock transport is the default; live integration is additive.** The published
   numbers must reproduce offline and deterministically, which a network-dependent
   result cannot do.

**Naming why you did not use the obvious thing reads as judgement.** This section is
the highest-value paragraph in the document — a panel will read it as evidence of how
you make decisions, which is what they are actually hiring for.

Also worth a sentence each: why incremental rather than gross; why common random
numbers; why three decline classes; why the RBI framework binds an acquirer and not
only its merchants.

### `WHAT_BROKE.md`

Finalised from incidents recorded as they happened, not reconstructed now.

The checklist requires a real incident **with a number attached** — the retry storm,
the idempotency bug, the confidently-wrong scorer, or whatever phase 7 turned up.
**Not a dependency conflict.** A dependency conflict as the headline incident reads as
having nothing real to report.

For each: what happened, the number, how it was found, the fix, and what it changed
about the design.

### The clean-clone reproduction gate

This is the real exit criterion for the whole project.

```bash
python -m recovery.cli repro
```

Clones into a temp directory, **creates a fresh virtualenv there**, installs into it,
runs `eval` from the committed seed and cassettes, and diffs against `data/results/`.
**Any drift fails loudly.**

The fresh venv is the point: it proves `pyproject.toml` actually declares everything
the project needs. A gate that reuses the development `.venv/` — or the global
interpreter, where `numpy` and `pytest` happen to be installed on this machine —
would pass even with a missing dependency, and a reviewer cloning the repo would then
hit the `ImportError` you did not.

It must pass **offline, with no `ANTHROPIC_API_KEY`, with no Razorpay keys.** That is
the whole point of the cassette design from phase 5: a reviewer clones the repo and
gets the published numbers without credentials.

The README claim and the actual output must match byte for byte. If they do not, the
README is wrong — fix the README, do not adjust the tolerance.

### The video — five minutes

| Time | Content |
|---|---|
| 45s | Problem, with a real number |
| 60s | Architecture |
| 90s | Live run |
| 60s | Results — **including where it underperformed** |
| 30s | One blocked action and the rule behind it |

Two rules, both non-negotiable:

- **Show the failing case.** Every published bar in this competition rewards honesty,
  and a demo with no failure reads as a demo with a hidden failure.
- **Put the number on screen as a number, with the control arm beside it.** Do not
  narrate over a scrolling terminal. The single most persuasive artifact available is
  a rupee figure next to its counterfactual.

Keep compliance to its 30 seconds: show one blocked action, name the rule, move on. A
panel of engineers will be impressed you encoded the regulation and will glaze over if
the pitch becomes a compliance lecture. The measured recovery number stays the
headline.

---

## Invariants this phase must not violate

- **No secrets committed** — check the full history, not just the working tree.
- **Every number in the README appears identically in `data/results/`.** If they
  disagree, the README is wrong.
- **The repro tolerance is never loosened to make the gate pass.**
- **The reproduction instructions are exactly what `repro` runs.**
- **The published numbers come from the mock transport and cassettes**, offline, with
  no credentials.
- **Underperformance is reported, not hidden** — in the README, and in the video's
  results slice.

This phase writes no application code, so it adds no tests. The Verification table
below stands in their place, and `pytest` staying green is a prerequisite for it.

---

## Verification

| Command | Checks |
|---|---|
| `pytest` | Policy rules, RBI constraints, idempotency, metrics maths, cohort determinism |
| `python -m recovery.cli eval --seed 42 --n 500` | Full run on the committed seed — must reproduce published numbers **exactly, offline, from cassettes, with no API key** |
| `python -m recovery.cli repro` | Clean clone into a temp dir, install, run, diff against `data/results/`. Any drift fails loudly |
| `python -m recovery.cli sweep --param p_self_heal --range 0.5,1.5` | The **sign** of the incremental lift must be stable — and if it is not, the README says so |

---

## Submission checklist

- [ ] Public repository, clean history, **no secrets committed**
- [ ] README leads with the results table and the honest caveat
- [ ] `ARCHITECTURE.md` covers all seven layers and both departures from the obvious
      approach
- [ ] `WHAT_BROKE.md` describes a real incident **with a number attached** — the retry
      storm, the idempotency bug, or the confidently-wrong scorer. Not a dependency
      conflict
- [ ] Blocked-action log visible and queryable
- [ ] Seed and cassettes committed; clean clone reproduces published numbers
- [ ] Five-minute video, structured as above, **showing a failure**
- [ ] Submitted 4 September

---

## Exit criteria

- [ ] `python -m recovery.cli repro` passes from a genuinely clean clone, offline,
      with no credentials in the environment
- [ ] Every number in the README appears identically in `data/results/`
- [ ] `git log` is clean and `git grep -i "sk-ant\|rzp_live\|rzp_test"` finds nothing
- [ ] The video is recorded, is under five minutes, and shows a failing case
- [ ] Submitted, with the form fields and deadline confirmed on the application page

---

## Pitfalls

- **Reconstructing `WHAT_BROKE.md` at the end.** It reads as reconstructed. If the
  incidents were not recorded as they happened, say what actually happened rather than
  inventing a narrative — a thin but true incident report beats a polished invention.
- **A README number that does not match the output.** The most damaging possible
  error, because it undermines the one thing the whole project is built to establish.
  The clean-clone gate exists to catch it; run it before writing the final numbers in.
- **Loosening the repro tolerance to make the gate pass.** Fix the README instead.
- **Committing a secret.** Check `git log` and the full history, not just the working
  tree. `.env` should never have been tracked; verify.
- **A five-minute video that runs to eight.** Rehearse against the clock. The 90-second
  live-run slice is where overruns happen; pre-run the command and cut to the result.
- **Narrating the number instead of showing it.** Put it on screen.
- **Hiding the underperformance.** The 60-second results slice explicitly includes
  where it underperformed. Every bar in this competition, the "what broke" field, and
  the job description's "understand root causes when targets are not met" all converge
  on screening for this. It is the cheapest available signal that you are the kind of
  engineer they are looking for.
- **Missing the deadline because it was never verified.** The 5 September date comes
  from secondary reporting. The official page renders dynamically and did not expose
  it to automated fetching; two other sources returned a 503 and a network-filter
  redirect. **Confirm on the application form itself** — and do not spend time
  re-attempting those three URLs. Target the 4th; the 5th is buffer, not plan.
