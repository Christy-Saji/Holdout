# Phase 5 — Agent layer

> Track 03 · Razorpay AI Buildathon · rebased target **Mon 1 September**
> Master spec: `docs/prompts/track-03-build-plan.md` §2.1, §6
> Previous phase: `phase-04-policy-engine-and-rbi.md` · Next: `phase-06-measurement-rigour.md`
> **Requires `ANTHROPIC_API_KEY`.** This is the project's one hard credential blocker.

---

## Prompt

> You are building the Razorpay AI Buildathon Track 03 submission — an AI revenue
> recovery system whose headline deliverable is **incremental** money recovered
> against a holdout control arm.
>
> This phase adds the agent arm: a Claude-driven recovery agent whose every money and
> contact action passes through the policy engine built in phase 4, plus a cassette
> record/replay layer so the published numbers reproduce offline with no API key.
>
> **Read §2.1 of the build plan, or the "Why not the Claude Agent SDK" section below,
> before you write anything.** This project deliberately uses the Anthropic SDK tool
> runner rather than the Claude Agent SDK, reversing the research brief's
> recommendation. Do not silently revert that decision.
>
> Read `CLAUDE.md` for the project invariants. Everything else you need is below.
>
> **Standing rule for this project: show your plan in your reply as visible text and
> wait for explicit approval before creating or editing any implementation file.**

---

## Prerequisites

- Phase 4 complete: `pytest tests/test_policy_rbi.py` passes and
  `--arms control,baseline` produces a real incremental number
- `.venv/` activated, then `python -m pip install anthropic` — **into the venv, never
  globally.** Confirm with `python -c "import sys; print(sys.prefix)"`
- **`ANTHROPIC_API_KEY` set.** Check with `ant auth status` first: an unset env var
  does not necessarily mean no credentials, since the SDK also resolves an OAuth
  profile from `ant auth login`. A bare `anthropic.Anthropic()` works with either.
- If no credential exists at all, this phase **hard-stops**. Phases 6 and 8 can
  proceed against control and baseline only, and the submission still stands — it
  just loses its most interesting arm.

---

## Why not the Claude Agent SDK — read before coding

The research brief recommends building on the Claude Agent SDK because Razorpay's
Agent Studio is built on it, so the architecture conversation lands in Razorpay's
vocabulary. The reasoning is sound; the conclusion is wrong.

**The Claude Agent SDK (`claude-agent-sdk`) is Claude Code packaged as a library:**
built-in Read/Write/Edit/Bash/Glob/Grep tools, a filesystem harness, context
management for coding work. It is a batteries-included *coding* agent. Using it to
decide whether to send an SMS at 14:00 means spinning up a coding harness to make a
scheduling decision, and it invites an obvious and damaging panel question with no
good answer.

**The right surface is the tool runner in the regular Anthropic SDK:**
`client.beta.messages.tool_runner` with `@beta_tool`-decorated functions. It drives
the agentic loop over *tools we define*, with no built-in tools and no sandbox.

Critically, Anthropic's documentation states that human-in-the-loop approval does
**not** require a manual loop — you *gate inside the tool function* and return a
refusal result the model must react to. That is exactly the seam this project needs:

- Every money or contact action is a tool.
- Every such tool calls `PolicyEngine.evaluate()` before doing anything.
- A denial returns `{allowed: false, rule_id, reason}` to the model, which must then
  choose a different action.
- The denial is written to the ledger — **the blocked-action log builds itself.**

The Razorpay-vocabulary benefit is preserved by naming the choice explicitly in
`ARCHITECTURE.md` and explaining the reasoning. Stating why you did *not* use the
obvious thing reads as judgement. Silently using the wrong thing reads as not
knowing the difference.

Do **not** write a manual `while stop_reason == "tool_use"` loop either. The runner
covers approval gating, logging, interception and result modification through its
per-turn hooks; hand-rolling the loop is a step backwards and the build plan calls
it out by name.

---

## Deliverables

| File | Contents | Spec |
|---|---|---|
| `recovery/agent/tools.py` | Six `@beta_tool` functions; three gate internally | §6 |
| `recovery/agent/runner.py` | `tool_runner` loop + cassette record/replay | §6 |
| `recovery/agent/prompts.py` | System prompt, cached stable prefix | §6 |
| `recovery/arms/agent.py` | The policy-gated agent arm | §8 |
| `tests/test_agent_gating.py` | A denied tool never reaches the transport | §10 |
| `data/cassettes/` | Recorded decisions, committed | §6 |

---

## Specification

### API surface — verified, do not improvise

```python
import anthropic
from anthropic import beta_tool

client = anthropic.Anthropic()          # zero-arg; resolves key or OAuth profile

@beta_tool
def schedule_retry(case_id: str, at_hour: int, idempotency_key: str) -> str:
    """Attempt a re-debit on a failed payment.

    Args:
        case_id: The case to retry.
        at_hour: Hour offset within the 336-hour window.
        idempotency_key: Unique key; the same key never executes twice.
    """
    ...

runner = client.beta.messages.tool_runner(
    model="claude-opus-5",
    max_tokens=16000,
    thinking={"type": "adaptive"},
    tools=[get_case_detail, get_customer_history, check_policy,
           schedule_retry, send_message, close_case],
    messages=[{"role": "user", "content": case_brief}],
)
for message in runner:
    ...
```

Notes that matter:

- **Model is `claude-opus-5`.** Exact string, no date suffix.
- **`thinking={"type": "adaptive"}`.** `budget_tokens` is **removed** on Opus 5 and
  returns a 400. If you recall `{"type": "enabled", "budget_tokens": N}` from
  training, that prior is stale.
- Tool schemas are generated from the function signature and docstring, so write real
  docstrings with an `Args:` block — they are the tool description the model reads.
- `runner.until_done()` is the one-shot variant if you do not need per-turn hooks.
- Optional: `output_config={"effort": "medium"}` to trade thoroughness for token
  spend. `high` is the default. Worth a sweep if cost becomes a concern.

### `recovery/agent/tools.py` — the tool surface

| Tool | Gated | Purpose |
|---|---|---|
| `get_case_detail(case_id)` | no | Decline reason, amount, method, attempt history |
| `get_customer_history(customer_id)` | no | Prior failures, recoveries, contacts received |
| `check_policy(action)` | no | **Read-only preview** — lets the model plan instead of trial-and-erroring against denials |
| `schedule_retry(case_id, at, idempotency_key)` | **yes** | Attempt a re-debit |
| `send_message(case_id, channel, template, at, idempotency_key)` | **yes** | SMS / WhatsApp / email nudge |
| `close_case(case_id, reason)` | no | Terminal — stop working this case |

**`check_policy` is worth calling out on video.** Giving the model a dry-run gate
means the sensible path is *plan within the constraints*, so a denial on a real
action becomes a genuine surprise worth logging rather than routine noise. Without
it, the blocked-action log fills with the model discovering the rules by bumping into
them, which is much less interesting evidence.

Gating happens **inside** the tool function, not in a wrapper the model cannot see:

```python
decision = engine.evaluate(action, case, ledger, clock)
ledger.append(kind="policy_check", verdicts=decision.verdicts,
              allowed=decision.allowed, ...)
if not decision.allowed:
    denied = decision.first_denial()
    return json.dumps({"allowed": False, "rule_id": denied.rule_id,
                       "reason": denied.reason})
# only now touch the transport
```

The denial is a **normal return value**, not an exception. The model must read it and
choose differently — that is the whole design. Raising would abort the turn and
teach the model nothing.

Tools return JSON strings. Parse tool inputs with `json.loads()`, never raw string
matching: Opus 5 may vary JSON escaping in tool inputs.

### `recovery/agent/prompts.py` and caching

Prompt caching is a **prefix match**, and the render order is `tools` → `system` →
`messages`. Any byte change anywhere in the prefix invalidates everything after it.
So:

- **Stable prefix, cached:** the system prompt, the recovery playbook, and the full
  policy-rule summary. These never change within a run.
- **After the last cache breakpoint:** the per-case brief — case id, amount, reason,
  attempt history, current hour.
- Max 4 breakpoints per request.
- **Verify it works:** `usage.cache_read_input_tokens` must be non-zero across
  repeated requests. If it is zero, something in the prefix is varying — a timestamp,
  an unsorted dict, a tool list built in a different order. Sort the tool list.

Do not put `datetime.now()` anywhere in the system prompt. It is the classic silent
cache invalidator and it also breaks determinism.

### `recovery/agent/runner.py` — cassettes

Every LLM decision is recorded to `data/cassettes/`, keyed by a hash of the request.

- **Default: replay.** `eval` runs from cassettes, so the published numbers reproduce
  **exactly, offline, with no API key**. A reviewer can clone the repo and rerun it
  without credentials. This is the single strongest reproducibility signal available
  in a submission like this one, and it directly serves the requirement that someone
  must be able to clone and rerun you.
- `--record` re-records. `--live` bypasses cassettes entirely.
- Key on a **canonical** hash of the full request — model, tools, system, messages —
  serialised with `sort_keys=True`. An unsorted key means a cassette miss on a
  logically identical request, which silently turns a replay run into a live run.
- On a cassette miss in replay mode, **raise**. Do not silently fall through to a live
  call: that would mean a reviewer without a key gets a crash instead of a wrong
  number, which is the right failure.

**The runner keeps its own message history and does not expose it.** If you need the
transcript for cassettes or the ledger, mirror it as you iterate:

```python
for message in runner:
    messages.append({"role": "assistant", "content": message.content})
    tool_response = runner.generate_tool_call_response()   # cached; tools run once
    if tool_response is not None:
        messages.append(tool_response)
```

`generate_tool_call_response()` is cached, so calling it does not re-execute tools.

**`pause_turn` caveat:** the Python runner does not auto-resume a `pause_turn` — it
exits and returns the paused message as final, with no error. This project uses no
server-side tools, so it should not arise; assert `stop_reason != "pause_turn"` and
fail loudly rather than silently truncating an agent's plan.

### Cost control

- `--n` bounds cohort size. Develop against `--n 50`, publish on 500.
- Prompt caching on the stable prefix.
- Cassette replay means the expensive path runs **once per design change**, not once
  per test run. Record once, then iterate for free.
- Consider `output_config={"effort": "medium"}` for the per-case decisions; they are
  not hard reasoning problems.

---

## Invariants this phase must not violate

- **The Anthropic SDK tool runner, not the Claude Agent SDK, and not a manual loop.**
- **Every money or contact tool gates internally** via `PolicyEngine.evaluate()`
  before touching the transport.
- **Denials are return values, not exceptions.**
- **Every evaluation is written to the ledger, allow and deny.**
- **The agent never sees latents.** It observes only through the phase-3 transport
  interface.
- **Replay-mode cassette misses raise.** Never silently fall through to a live call.
- **No `datetime.now()`** in prompts, cassette keys, or ledger timestamps.

---

## Tests

`tests/test_agent_gating.py` — these run offline against cassettes or stubs; none
should require an API key.

- **The load-bearing test:** a `schedule_retry` on a HARD decline returns
  `{"allowed": false, ...}` and the transport records **zero** attempts. Assert on the
  transport, not just on the return value — a gate that logs a denial and then acts
  anyway would pass a weaker test.
- The same assertion for `send_message` inside quiet hours.
- A denial writes exactly one `policy_check` ledger entry with `allowed=False` and a
  populated `rule_id`, and **no** `action` entry.
- `check_policy` is side-effect free: calling it writes no `action` entry and changes
  no transport state.
- Cassette replay determinism: two replay runs over the same cohort produce identical
  per-case outcomes.
- A cassette miss in replay mode raises rather than calling the API. Assert this with
  no key set in the environment.
- Tool inputs are parsed with `json.loads`, and a tool given malformed JSON returns an
  error result rather than raising through the runner.

---

## Exit criteria

- [ ] `python -m recovery.cli eval --seed 42 --n 500 --arms control,baseline,agent --record`
      completes and produces a **first agent-arm number**
- [ ] The same command **without** `--record` reproduces that number exactly from
      cassettes
- [ ] With `ANTHROPIC_API_KEY` unset, the replay run still succeeds
- [ ] The blocked-action log has real entries with `rule_id` populated:
      `grep -c '"allowed": false' data/results/<run_id>/agent.jsonl` is > 0
- [ ] `usage.cache_read_input_tokens` is non-zero on the second and later requests of
      a recording run
- [ ] `pytest tests/test_agent_gating.py` passes with no API key present
- [ ] `grep -rn "claude_agent_sdk\|claude-agent-sdk" recovery/` returns nothing
- [ ] `grep -rn "budget_tokens" recovery/` returns nothing

**From this point the repo is submittable.** Phases 6, 7 and 8 strengthen it; none of
them is required for it to cohere.

---

## Pitfalls

- **Reverting to the Claude Agent SDK.** It is the research brief's recommendation and
  it will look like the obvious choice to anyone reading the brief and not §2.1. It is
  the wrong tool for a scheduling decision and the reasoning is in this file.
- **Hand-rolling the agentic loop.** The build plan names this as an anti-pattern. The
  runner's per-turn hooks cover approval gating, interception and result modification.
- **`budget_tokens` on Opus 5.** Returns a 400. Use `thinking={"type": "adaptive"}`.
- **A cassette key that is not canonical.** Unsorted JSON in the key means logically
  identical requests miss, and in a mode that falls through to live calls that turns a
  reproducible run into an expensive non-reproducible one. Sort keys, and raise on
  miss.
- **Silently falling through on a cassette miss.** The whole reproducibility claim
  rests on replay being airtight. Fail loudly.
- **Gating in a wrapper instead of in the tool.** If the gate lives outside the tool
  function, the model never sees the denial, cannot adapt, and the blocked-action log
  stops being evidence of an agent reasoning within constraints.
- **A zero-entry blocked-action log.** If `check_policy` is doing its job the agent
  will rarely be denied — but zero denials across 500 cases usually means the gate is
  not wired in, not that the agent is perfect. Verify by forcing a HARD-decline case
  through.
- **Burning the budget on a recording run.** Record on `--n 50` first, confirm the
  loop behaves, then record the full 500 once.
- **Prompt cache silently not working.** Check `cache_read_input_tokens`. Zero means
  you are paying full price on every call and the prefix is varying.
