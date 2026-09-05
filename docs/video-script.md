# Five-minute video — shot list and script

Track 03 · Razorpay AI Buildathon. Structure fixed by the build plan §9 day 7.

Two rules, both non-negotiable, both from the spec:

- **Show the failing case.** Every published bar in this competition rewards honesty,
  and a demo with no failure reads as a demo with a hidden failure.
- **Put the number on screen as a number, with the control arm beside it.** Do not
  narrate over a scrolling terminal. The single most persuasive artifact available is a
  rupee figure next to its counterfactual.

Total 4:45, leaving 15 seconds of slack. Every number below is in `data/results/` and on
`docs/index.html` — read them off the screen, do not recite from memory.

---

## 0:00 – 0:45 · The problem, with a real number

**On screen:** `docs/index.html`, the hero block. Two figures side by side —
₹166,551 gross, ₹82,931 incremental.

> "A subscription payment fails. Everyone in this space builds the same thing: retry it,
> message the customer, report how much you recovered. This system recovered
> ₹1,66,551 across 500 failed payments.
>
> That number is a lie, and it's the specific lie this project is about.
>
> Because we also ran a holdout — 500 identical cases where we did *nothing at all*. The
> holdout recovered ₹83,620 on its own. Cards get topped up. Payday arrives. Transient
> issuer errors clear. Twenty per cent of failed payments heal with no help from anyone.
>
> So the honest number isn't ₹1,66,551. It's ₹82,931 — the incremental. Anyone reporting
> the gross figure is claiming credit for **twice the money they actually caused**."

**Beat.** Let the two numbers sit on screen together for a second before moving.

---

## 0:45 – 1:45 · Architecture

**On screen:** `ARCHITECTURE.md`, the seven-layer table. Then `recovery/policy/rules.py`.

> "Seven layers. The two that matter for this pitch:
>
> **The policy engine.** Every money movement and every customer contact routes through
> one `evaluate()` call. Twelve rules — nine operational, three RBI. And it **does not
> short-circuit**: every rule runs on every action even after one has already denied it,
> and the ledger records every verdict, pass and fail. That's deliberate. A
> short-circuiting engine gives you a cheaper evaluation and a useless audit log — you'd
> know an action was blocked, but not everything that was wrong with it. The denials
> *are* the blocked-action log.
>
> **Two departures from the obvious approach.** We used the Anthropic SDK's tool runner,
> not the Claude Agent SDK — the Agent SDK is a coding harness, and using it to decide
> whether to send an SMS at 2pm would be the wrong tool. The tool runner lets us put the
> approval gate *inside the tool function*, which is exactly the seam this system needs.
>
> And the mock transport is the default, not the live Razorpay integration. A
> network-dependent result doesn't reproduce. The published numbers come from the
> deterministic mock, offline, every time."

---

## 1:45 – 3:15 · The live run

**On screen:** a terminal. Run it live; do not play a recording.

```
python -m recovery.cli eval --seed 42 --n 500 --arms control,baseline
python -m recovery.cli report
```

> "Same cohort in both arms. Same hidden latents. And every random draw is keyed by case
> identity through a hash, not pulled from a sequential stream — so the two arms stay
> paired draw for draw, and the difference between them is the intervention rather than
> luck. That pairing is what makes the interval tight enough to quote."

**Then switch to `docs/index.html` → Replay.** This is the strongest 45 seconds
available; give it the room.

> "Every one of the 500 cases is replayable from the committed ledger. Pick one, scrub
> the fourteen-day window, and watch both arms hour by hour."

Scrub a lift case, then **filter to *had an action refused* and open `case-00002`** —
this is the failing case, and it is the one to show:

> "This is a ₹355 payment declined `lost_card`. Watch what the system does: at hour 24 it
> tries a retry, and `HardDeclineNoRetry` refuses it — *'lost_card is a permanent decline;
> retrying cannot succeed.'* At hour 48, refused again. This case never recovers, and it
> never should. We spent **nothing** on it.
>
> That's the system working. Now let me show you it failing."

---

## 3:15 – 4:15 · Results, including where it underperformed

**On screen:** the results table, then the sensitivity slider on `docs/index.html`.

> "₹82,931 incremental, 95% interval ₹61,719 to ₹1,05,546, two thousand paired bootstrap
> replicates. The interval clears zero. The treated arm also gets the cash **3.34 days
> sooner** — 3.7 days versus 7.
>
> Now the part that matters. That result rests on assumptions that are **our own priors,
> not quoted rates**. So we swept three of them ±50%."

Move the slider across `p_self_heal`, then switch to `attempt_decay`.

> "The sign survives in all three sweeps — nine out of nine points, every interval clearing
> zero. But look at this: `attempt_decay` moves the headline **more** than the self-heal
> rate does. From ₹50,420 to ₹96,668. That's the parameter this result is most fragile to,
> and it isn't the one we designed the critique around.
>
> **Three things underperformed, and I'd rather say them than have you find them.**
>
> One: **there is no agent-arm number.** The agent is built and tested — twenty-three tests,
> including proof that a policy-denied tool call never reaches the transport. It has never
> made a live API call, because the API key didn't arrive. Rather than publish a figure from
> an unrecorded arm, we report it as built, tested, and unrecorded.
>
> Two: **contact fatigue reports zero**, because the baseline arm is retry-only and never
> messages anyone. The metric is real; it has nothing to measure until the agent arm runs.
>
> Three: **the cohort is synthetic.** No real transaction data was available. This is a
> simulation result under a generative model we published in full, not live-money evidence."

---

## 4:15 – 4:45 · One blocked action, and the rule behind it

**On screen:** `WHAT_BROKE.md` incident 4, the cost table.

Keep this tight. A panel of engineers will be impressed you encoded the regulation and
will glaze over if the pitch becomes a compliance lecture.

> "One failure, with a number. We fed the system a case labelled `insufficient_funds`
> whose underlying truth was a dead instrument. The label is wrong at the input, so
> `HardDeclineNoRetry` can't fire — it reads the taxonomy, and the taxonomy says soft.
>
> It retried seven times over a week before the economics stopped it. **₹70 on a ₹4,000
> payment that could never succeed** — ₹21 in gateway fees, and ₹49 in modelled
> approval-rate damage, which is the larger number and the one that isn't on the invoice.
>
> The stopping rule worked — it bit at attempt eight without any attempt-count constant
> existing anywhere in the code. But it bit *seven attempts in*, because it can only be as
> good as the label it's fed. The fix isn't a better cap. It's a decline feed you can trust.
> We didn't build that, and pretending the cap covers the gap would be the wrong lesson.
>
> The identical case labelled `stolen_card` correctly costs **₹0.00**. The whole ₹70 is
> attributable to bad input, not to the policy."

**Last frame:** the two numbers again — ₹166,551 gross, ₹82,931 incremental.

> "Measure against a holdout, or you're reporting someone else's work as your own."

---

## Pre-flight

- [ ] `python -m recovery.cli repro` passes, on camera or immediately before
- [ ] `docs/index.html` open in a **second** window, already scrolled to Replay
- [ ] `case-00002` already located — do not search for it live
- [ ] Terminal font large enough to read at 720p
- [ ] Under five minutes on a real timed run-through, not an estimate
