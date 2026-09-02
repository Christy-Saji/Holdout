# CONTEXT.md — Razorpay AI Buildathon, Track 03

The full project contract and master prompt. `CLAUDE.md` carries only the rules that
must hold in every session; everything explanatory lives here and is read on demand.

**Reading order for a fresh session:** `CLAUDE.md` → this file → the single phase file
you are executing (`docs/prompts/phase-NN-*.md`). The build plan
(`docs/prompts/track-03-build-plan.md`) is the authoritative spec and wins wherever it
disagrees with anything else.

---

## Master prompt

> Paste this into a fresh session to bootstrap it.

> You are working on a submission to the **Razorpay AI Buildathon, Track 03 — AI
> Revenue Recovery**, in `C:\Users\chris\college\programming\project\razorpay`.
>
> It is a Python project that measures how much money an AI agent recovers from failed
> recurring payments — reported as **incremental** recovery against a holdout control
> arm, not gross recovery, plus cost per *incremental* rupee. A policy engine gates
> every money and contact action, including three rules from the RBI E-mandate
> Framework 2026, and every evaluation is written to an append-only hash-chained
> ledger.
>
> Read `CLAUDE.md` for the invariants, then `CONTEXT.md` for the background, then the
> phase file for the work you are doing. `docs/prompts/track-03-build-plan.md` is the
> authoritative spec.
>
> Three things are load-bearing and easy to break: all money is **integer paise**,
> unknown decline codes **raise** rather than defaulting to soft, and every random
> draw comes from the **blake2b common-random-numbers helper** rather than a
> sequential RNG.
>
> Install everything into `.venv/`, creating it if absent. Never install globally.
>
> Show your plan in your reply as visible text and wait for explicit approval before
> creating or editing any implementation file.

---

## 1. What this project is

This is not a prize hackathon. There is no prize pool and no leaderboard. It is a
hiring filter for a **₹75,000/month AI Builder internship** in Bengaluru, 6 or 12
months, starting September, open to the 2027–2029 graduating batches. You build alone.
Strong submissions go straight to a panel — no resume screen, no aptitude test, no
group discussion.

That changes the optimisation target. The goal is not to impress a judge for ninety
seconds. It is to make a Razorpay engineer read the repo and think *this person could
own a money-moving service on my team in September.*

**Target submission: 4 September 2026. Deadline: 5 September.** The 5th is buffer, not
plan.

## 2. The judged bar

Four artifacts are submitted:

1. A public repository
2. A five-minute pitch video
3. An architecture write-up
4. A written answer to *"What broke and how you got out"*

Track 03's published requirement, verbatim:

> "Don't just identify the problem. Show **measured money recovered across a batch**,
> with **compliant escalation**, **stopping rules**, and an **audit trail**."

Each phrase maps to something in the repo: the measured number comes from the three-arm
eval harness, compliant escalation from the RBI rules in the policy engine, stopping
rules from `AttemptCap` and `BudgetCeiling`, and the audit trail from the hash-chained
ledger.

## 3. The core bet

Nearly every competing submission will report a **gross** number: *"we recovered ₹4.2
lakh."* That figure is close to meaningless, because a meaningful share of those
payments would have recovered on their own — customers retry, cards get topped up,
temporary issuer blocks clear. Reporting gross recovery as if the agent caused it is
exactly the "cherry-picked match proves nothing" failure Razorpay pre-emptively calls
out in Track 04's bar.

**We report incremental recovery against a holdout control arm, and cost per
*incremental* rupee.** This is standard practice on any payments growth or data science
team, so it reads instantly as someone who already thinks the way they do. Almost no
student will do it.

This is the differentiator. Everything else in the repo exists to make this number
credible.

## 4. Why common random numbers matter

All three arms run against the **same cohort** with the **same latents**, and every
stochastic draw is derived deterministically from the event's identity:

```
u(seed, case_id, event_kind, sequence) =
    int.from_bytes(blake2b(f"{seed}|{case_id}|{event_kind}|{sequence}").digest()[:8]) / 2**64
```

The effect: the hour at which case #237 would self-heal is *identical* in the control
arm and the agent arm. The difference between arms is therefore attributable purely to
the intervention rather than to sampling noise, which dramatically tightens the
confidence interval on incremental lift for the same cohort size. It also means arms
can run in any order, in parallel, or in separate processes, and the comparison stays
valid.

Replacing this with `random.random()` or a seeded `numpy.Generator` drawn in sequence
silently destroys the pairing and invalidates every published number.

## 5. Architecture decisions that must not be silently reverted

Each of these reverses something a reasonable reader would otherwise assume. If you
find yourself about to "fix" one of them, read the reasoning first.

### Anthropic SDK tool runner, not the Claude Agent SDK

The research brief recommends the Claude Agent SDK on the grounds that Razorpay's Agent
Studio is built on it. That conclusion is wrong and was deliberately reversed.

The Claude Agent SDK is Claude Code packaged as a library — built-in Read/Write/Edit/
Bash tools, a filesystem harness, context management for coding work. Using it to
decide whether to send an SMS at 14:00 means spinning up a coding harness to make a
scheduling decision, and invites an obvious panel question with no good answer.

The right surface is `client.beta.messages.tool_runner` with `@beta_tool`-decorated
functions: the agentic loop over tools *we* define, no built-in tools, no sandbox.
Anthropic's own docs state that approval gating belongs **inside the tool function** —
return a refusal the model must react to — which is exactly the policy-engine seam this
project needs, and it yields the blocked-action log for free.

The Razorpay-vocabulary benefit is preserved by naming the choice and its reasoning in
`ARCHITECTURE.md`. Stating why you did *not* use the obvious thing reads as judgement;
silently using the wrong thing reads as not knowing the difference.

### Mock transport is the default; live Razorpay integration is additive

Every arm runs against a deterministic mock transport. The eval harness never depends
on the network or on credentials. Live test-mode integration is a *demonstration* of
real API wiring, not a load-bearing dependency of the results. If keys never arrive,
the submission still stands.

### Three decline classes, not two

SOFT / HARD / **ACTION**, refining the brief's binary hard-vs-soft split. Expired
cards, revoked mandates and failed 3DS can never be fixed by a silent retry but are not
permanently dead either — contact is the only move. ACTION reasons get
`retry_success_base = 0.0` but **non-zero** `self_heal_rate`, and that non-zero
self-heal is precisely what inflates naive gross-recovery numbers. Modelling it is
load-bearing, not decorative.

## 6. Repository layout

Annotated with the phase that creates each item.

```
razorpay/
├── README.md                    # P8 — results table + caveats above the fold
├── ARCHITECTURE.md              # P8 — the write-up deliverable
├── WHAT_BROKE.md                # P7/P8 — drafted as incidents happen
├── CLAUDE.md                    # session rules — kept lean
├── CONTEXT.md                   # this file
├── Makefile                     # P1 — delegates to the CLI
├── pyproject.toml               # P1
├── .env.example                 # P1
├── docs/prompts/                # build plan + 8 phase files
├── data/
│   ├── cohort_seed42.jsonl      # P2 — committed; reviewers get identical input
│   ├── cassettes/               # P5 — recorded LLM decisions
│   └── results/                 # P6 — committed run outputs + figures
├── recovery/
│   ├── cli.py                   # P1 — primary entry point
│   ├── declines.py              # P2 — decline taxonomy
│   ├── cohort.py                # P2 — L1 generator + latents + CRN helper
│   ├── ledger.py                # P2 — L6 append-only hash-chained ledger
│   ├── scoring.py               # P4 — L2 P(recover | intervention), expected value
│   ├── policy/{engine,rules,rbi}.py    # P4 — L3
│   ├── agent/{tools,runner,prompts}.py # P5 — L4
│   ├── transport/
│   │   ├── base.py, mock.py     # P3 — L5 deterministic simulator
│   │   └── razorpay_test.py     # P7 — L5 live test mode (optional)
│   ├── arms/
│   │   ├── control.py           # P3 — do nothing
│   │   ├── baseline.py          # P4 — Razorpay T+3 default
│   │   └── agent.py             # P5 — policy-gated agent
│   └── eval/
│       ├── harness.py           # P3 — L7 three-arm runner
│       ├── metrics.py           # P3 v0, P6 bootstrap
│       └── report.py            # P6 — markdown + matplotlib
├── tests/                       # P2–P6
└── scripts/make_figures.py      # P6
```

## 7. Phase map and schedule

`docs/prompts/track-03-build-plan.md` is **authoritative**. The phase files are
executable slices of it, each self-contained enough to hand to a fresh session. Where a
phase file and the build plan disagree, the build plan wins — and the disagreement is a
bug in the phase file worth fixing.

| Phase | File | Build-plan sections |
|---|---|---|
| 1 | `phase-01-scaffold-and-tooling.md` | §8 |
| 2 | `phase-02-domain-core.md` | §3.1, §4.1–4.3 |
| 3 | `phase-03-simulation-and-harness.md` | §3.2, §3.3, §7 (partial) |
| 4 | `phase-04-policy-engine-and-rbi.md` | §5 |
| 5 | `phase-05-agent-layer.md` | §2.1, §6 |
| 6 | `phase-06-measurement-rigour.md` | §3.4, §7 |
| 7 | `phase-07-live-integration-and-adversarial.md` | §2.2, §9 day 5 |
| 8 | `phase-08-artifacts-and-submission.md` | §9 days 6–7, §10, §12 |

The build plan's day-by-day schedule (§9) has **slipped one day**: its Day 1 was Friday
29 August and nothing was written that day. Rebased against a 4 September submission:

| Date | Phases |
|---|---|
| Sat 30 Aug | 1, 2, 3 |
| Sun 31 Aug | 4 |
| Mon 1 Sep | 5 (needs API key) |
| Tue 2 Sep | 6 |
| Wed 3 Sep | 7 (optional) + start 8 |
| Thu 4 Sep | finish 8, record video, submit |

Phase 7 is the **drop-first phase**. It is the only credential-dependent one and its
failure mode is a weaker demo, not a broken submission. The repo is submittable from
the end of phase 5 onward; every later phase strengthens it rather than being required
for coherence.

## 8. Environment gotchas

Verified on this machine, 30 August 2026.

- **Platform:** Windows 11. PowerShell is primary; Git Bash is available and is what
  most commands have been run through.
- **Python 3.11.9**, pip 24.0. Node 22.17.1 is installed but unused — this is a Python
  build.
- **`make` is NOT installed.** Use `python -m recovery.cli <cmd>`.
- **`uv` is not installed.**
- **No virtualenv exists yet.** Creating `.venv/` is the first action of phase 1.
- **Installed globally:** `numpy` 2.4.6, `pytest` 9.0.3 — but a global install does
  **not** make them available to this project. Expect to install them again inside
  `.venv`. **Not installed anywhere:** `anthropic`, `razorpay`, `matplotlib`.
- **`git rev-parse --abbrev-ref HEAD` fails** while HEAD is unborn (no commits yet).
  Use `git symbolic-ref --short HEAD` until the first commit exists.
- **Network filter:** this connection sits behind a Christ University Sophos content
  filter that silently 307-redirects some job and aggregator domains. An unexpected
  redirect to `sophoscukc.christuniversity.in` is the filter, not the site being down.
- **Bash heredoc trap:** the Bash tool wraps commands in `bash -c '...'`, so
  apostrophes in prose content terminate the outer wrapper and produce `unexpected EOF
  while looking for matching quote`. For prose-heavy files, use the Write tool.
- **Empty directories are invisible to git.** `recovery/`, `tests/`, `data/` and
  `scripts/` exist on disk but will not appear in `git status` until they contain
  files.

## 9. Open blockers

| Blocker | Needed by | Failure mode |
|---|---|---|
| `ANTHROPIC_API_KEY` not obtained | Phase 5 | **Hard stop** — the agent arm cannot run |
| Razorpay test-mode keys not obtained | Phase 7 | Weaker demo only; phase is droppable |
| Deadline not independently verified | — | 5 September comes from secondary reporting. The official page renders dynamically and did not expose the date to automated fetching; two other sources returned 503 and a filter redirect. **Confirm on the application form.** Do not re-attempt those three URLs |

Cost assumptions (retry ≈ ₹3, SMS ≈ ₹0.20, WhatsApp ≈ ₹0.35, email ≈ ₹0.01) and all
decline population weights are **our own priors, not quoted rates**. They are swept in
the sensitivity analysis, and the README must say so plainly.
