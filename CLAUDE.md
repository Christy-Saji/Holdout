# CLAUDE.md

Razorpay AI Buildathon, **Track 03 — AI Revenue Recovery**. A Python project that
measures how much money an AI agent recovers from failed recurring payments, reported
as **incremental** recovery against a holdout control arm. Submit 4 September 2026.

| Need | Read |
|---|---|
| Background, master prompt, repo map, schedule, blockers | `CONTEXT.md` |
| The authoritative spec | `docs/prompts/track-03-build-plan.md` |
| The work you are doing now | `docs/prompts/phase-NN-*.md` (8 phases) |

The build plan wins wherever it disagrees with anything else.

---

## Non-negotiable invariants

Violating any of these is a correctness bug, not a style preference.

- **All money is integer paise. Never float rupees.**
- **Unknown decline reason codes raise.** Never default to `SOFT` — that is how a
  dunning system starts retrying stolen cards.
- **Every stochastic draw comes from the blake2b CRN helper**, keyed by
  `(seed, case_id, event_kind, sequence)`. Never a sequential RNG stream: it silently
  destroys the pairing between arms and invalidates every published number.
- **Latents are hidden.** `Case.latents` is read only by the simulator — never by an
  arm, the scorer, or the policy engine.
- **Every money or contact action routes through `PolicyEngine.evaluate()`.** No
  bypass in a test helper or a debug path.
- **The policy engine does not short-circuit.** All rules run; the ledger records
  every verdict, pass and fail. The denials are the blocked-action log.
- **The ledger is append-only and hash-chained.** No update path, no delete path.
- **Timestamps are the simulated clock**, never `datetime.now()`.
- **No dashboard, no web UI.** Out of scope; adds demo risk and earns zero points.
- **No secrets committed.** `.gitignore` excludes `.env` and `.venv/`.

Three architecture decisions must not be silently reverted — the **Anthropic SDK tool
runner** rather than the Claude Agent SDK, the **mock transport** as default, and
**three decline classes** rather than two. Reasoning in `CONTEXT.md` §5.

---

## Environment

**Every install goes into `.venv/`. If it does not exist, create it, then install into
it.** Never `pip install` globally, and never skip the venv because a package is
already there globally — a global install does not make it available to this project.

```bash
python -m venv .venv && source .venv/Scripts/activate   # Git Bash
python -m venv .venv ; .\.venv\Scripts\Activate.ps1     # PowerShell
python -m pip install -e ".[dev]"
```

Verify before installing or running: `python -c "import sys; print(sys.prefix)"` must
end in `.venv`.

Windows 11, Python 3.11.9. **`make` is not installed** — the Makefile exists only so
reviewers on Linux/macOS get `make eval`. `ANTHROPIC_API_KEY` is **not set** (hard
blocker for phase 5). Full environment notes in `CONTEXT.md` §8.

---

## Commands

```bash
python -m recovery.cli eval --seed 42 --n 500 --arms control,baseline,agent
python -m recovery.cli report
python -m recovery.cli sweep --param p_self_heal --range 0.5,1.5
python -m recovery.cli repro          # clean-clone reproduction gate

pytest                                # all tests
pytest tests/test_policy_rbi.py -v    # the compliance evidence
```

Agent-arm flags: default replays from `data/cassettes/` (offline, no API key);
`--record` re-records; `--live` bypasses cassettes entirely.

---

## Working agreement

**Show the plan in the reply as visible text and wait for explicit approval before
creating or editing any implementation file.** Approving a plan-mode exit does not
count. A previous session began writing `recovery/declines.py` straight after a
plan-mode approval and the write was interrupted for exactly this reason.

- **Record incidents in `WHAT_BROKE.md` as they happen**, not reconstructed at the
  end. The checklist needs a real incident with a number attached — a retry storm, an
  idempotency bug, a confidently-wrong scorer. A dependency conflict does not count.
- **Report findings honestly.** If the agent barely beats the baseline, that is the
  result. If the sign of the lift does not survive the sensitivity sweep, that is a
  stated limitation. The whole competition is screening for this.
