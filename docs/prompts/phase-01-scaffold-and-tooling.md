# Phase 1 — Scaffold and tooling

> Track 03 · Razorpay AI Buildathon · rebased target **Sat 30 August** (with phases 2–3)
> Master spec: `docs/prompts/track-03-build-plan.md` §8
> Previous phase: none · Next phase: `phase-02-domain-core.md`

---

## Prompt

> You are building the Razorpay AI Buildathon Track 03 submission — an AI revenue
> recovery system whose headline deliverable is **incremental** money recovered
> against a holdout control arm.
>
> This phase is the scaffold only. Create the packaging, the entry point, and the
> environment hygiene — no domain logic, no simulation, no policy rules, no agent.
> Every module you touch is either a config file or an empty-but-importable package.
>
> Read `CLAUDE.md` for the project invariants before you start. You do not need to
> read the master build plan for this phase; everything you need is below.
>
> **Standing rule for this project: show your plan in your reply as visible text and
> wait for explicit approval before creating or editing any implementation file.**
> Approval of a plan-mode exit does not count.

---

## Prerequisites

- Working directory `C:\Users\chris\college\programming\project\razorpay`
- `python --version` reports 3.11.x
- These directories already exist on disk (empty, therefore invisible to git):
  `recovery/{policy,agent,transport,arms,eval}`, `tests/`, `data/{cassettes,results}`,
  `scripts/`, `docs/prompts/`
- No credentials required

---

## Deliverables

| File | Contents | Spec |
|---|---|---|
| `.venv/` | Project virtualenv — **created first**, never committed | CLAUDE.md |
| `pyproject.toml` | Package metadata and dependencies | §8 |
| `.gitignore` | **Must** exclude `.env` | §8, §12 |
| `.env.example` | Key names only, never values | §8 |
| `Makefile` | Thin delegation over the CLI | §8 |
| `recovery/cli.py` | Argparse skeleton: `eval`, `report`, `sweep` | §8 |
| `recovery/__init__.py` + one per subpackage | Make the packages importable | §8 |
| `tests/__init__.py` | | |

---

## Specification

### The virtualenv — do this first

**Every dependency in this project is installed into a project-local virtualenv at
`.venv/`. If it does not exist, create it before installing anything.** Never
`pip install` into the global interpreter.

```bash
# Git Bash
python -m venv .venv && source .venv/Scripts/activate

# PowerShell
python -m venv .venv ; .\.venv\Scripts\Activate.ps1
```

Verify you are inside it before proceeding — `python -c "import sys; print(sys.prefix)"`
must print a path ending in `.venv`.

**A globally installed package is not available to this project.** `numpy` 2.4.6 and
`pytest` 9.0.3 exist in the global interpreter on this machine, but a fresh `.venv`
does not inherit them. Install them into the venv along with everything else, via
`python -m pip install -e ".[dev]"` once `pyproject.toml` exists.

`.venv/` is excluded by `.gitignore` and never committed.

### `pyproject.toml`

Setuptools backend, `requires-python = ">=3.11"`, project name `recovery`.

| Dependency | Why | Present on this machine? |
|---|---|---|
| `anthropic` | Tool runner — `client.beta.messages.tool_runner`, `@beta_tool` | **No** |
| `razorpay` | Test-mode SDK (phase 7) | **No** |
| `numpy` | Seeded RNG, bootstrap | Global only — 2.4.6 |
| `matplotlib` | Figures (phase 6) | **No** |
| `pytest` | Tests | Global only — 9.0.3 |

"Global only" means present in the machine's interpreter but **absent from a fresh
`.venv`**. Install everything into the venv regardless of what a bare
`python -m pip list` reports.

Put `pytest` under an optional `dev` extra rather than the runtime deps.
Do **not** pin exact versions; floor constraints only.

**Reuse, do not hand-roll.** The dependency table exists to prevent a hand-written
`while stop_reason == "tool_use"` loop, a raw-`requests` Razorpay client, or a
hand-rolled PRNG. Each of those would be a step backwards.

### `.gitignore`

At minimum: `.env`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.venv/`, `venv/`,
`*.egg-info/`, `.DS_Store`.

Do **not** ignore `data/` — `data/cohort_seed42.jsonl`, `data/cassettes/` and
`data/results/` are all committed on purpose, because a reviewer must be able to
clone and reproduce the published numbers offline.

Write this file **before** any credential exists to leak.

### `.env.example`

Key names and a comment each. Never a real value.

```
# Anthropic API key — required from phase 5 (the agent arm).
# Phases 1-4 and cassette replay do not need it.
ANTHROPIC_API_KEY=

# Razorpay TEST-MODE keys — required for phase 7 only, which is optional.
RAZORPAY_KEY_ID=
RAZORPAY_KEY_SECRET=
```

### `Makefile`

`make` is **not installed on this machine**. This file exists so a reviewer on Linux
or macOS gets the conventional entry points. It must contain no logic of its own —
every target is one line delegating to the CLI, so the two interfaces can never
drift apart.

Targets: `eval`, `report`, `sweep`, `test`, `repro`.

```make
.PHONY: eval report sweep test repro

eval:
	python -m recovery.cli eval --seed 42 --n 500

report:
	python -m recovery.cli report

sweep:
	python -m recovery.cli sweep --param p_self_heal --range 0.5,1.5

test:
	pytest

repro:
	python -m recovery.cli repro
```

(Tabs, not spaces, for the recipe lines.)

### `recovery/cli.py`

Argparse with three subcommands. This phase wires the parser and leaves each handler
as a stub that prints what it would do and exits 0 — later phases fill them in.

| Subcommand | Flags | Filled in by |
|---|---|---|
| `eval` | `--seed` (int, default 42), `--n` (int, default 500), `--arms` (comma list, default `control`), `--record`, `--live` | P3, P5 |
| `report` | `--run-id` (optional) | P6 |
| `sweep` | `--param` (str), `--range` (two comma-separated floats) | P6 |

Also add a `repro` subcommand stub for the Makefile target above; phase 8 implements
it.

Must run as `python -m recovery.cli`, so include the `if __name__ == "__main__":`
guard. Parse `--arms` into a list at parse time via `type=lambda s: s.split(",")`.

### Package `__init__.py` files

One in each of: `recovery/`, `recovery/policy/`, `recovery/agent/`,
`recovery/transport/`, `recovery/arms/`, `recovery/eval/`, `tests/`.

Empty is fine, except `recovery/__init__.py` which should carry a one-line
docstring naming the project. Creating these is also what makes the directories
visible to git for the first time.

---

## Invariants this phase must not violate

- **No secrets committed.** `.gitignore` excludes `.env` before `.env` can exist.
- **No dashboard, no web UI.** If a dependency implies a server (Flask, FastAPI,
  Streamlit), it does not belong here.
- **The CLI is the primary interface.** The Makefile never grows logic of its own.

---

## Tests

None in this phase. `pytest` should collect zero tests and exit cleanly, which
confirms the test package imports.

---

## Exit criteria

- [ ] `.venv/` exists, is activated, and
      `python -c "import sys; print(sys.prefix)"` prints a path ending in `.venv`
- [ ] `git status --porcelain` does **not** list `.venv/`
- [ ] `python -m recovery.cli --help` prints all four subcommands
      (`eval`, `report`, `sweep`, `repro`)
- [ ] `python -m recovery.cli eval --seed 42 --n 500` exits 0 with a stub message
- [ ] `python -c "import recovery, recovery.policy, recovery.agent, recovery.transport, recovery.arms, recovery.eval"`
      succeeds
- [ ] `pytest` collects 0 tests and exits 0
- [ ] `git status --porcelain` shows the new files, and **does not** list `.env`
- [ ] `grep -c "^\.env$" .gitignore` returns 1

---

## Pitfalls

- **`make --version` will fail.** That is expected and already known — do not try to
  install it or work around it. Use the CLI.
- **`git rev-parse --abbrev-ref HEAD` fails** while HEAD is unborn. Use
  `git symbolic-ref --short HEAD` until the first commit exists.
- **Empty directories do not appear in `git status`.** Until the `__init__.py` files
  land, `recovery/` and friends will look like they do not exist to git. They do
  exist on disk — do not recreate them.
- **Installing globally because a venv "seems like overhead".** It is a standing rule
  in this project: create `.venv/` and install into it, always.
- **Trusting a bare `pip list`.** It reports the global interpreter. `numpy` and
  `pytest` show up there and are still absent from a fresh venv. Check from inside the
  activated environment.
- **Deferring the heavy dependencies.** `anthropic`, `razorpay` and `matplotlib` are
  not needed until phases 5, 7 and 6 — declare them in `pyproject.toml` now, and
  install them into the venv when their phase arrives.
- **Do not scaffold empty module files for later phases.** An empty `policy/rules.py`
  is worse than no file: it imports cleanly and hides the fact that the phase has not
  been done.
