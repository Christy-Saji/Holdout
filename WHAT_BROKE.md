# What broke

Incidents recorded as they happened, not reconstructed afterwards. Each one names the
symptom, what it would have cost if it had shipped, and how it was caught.

---

## 1. Three RBI boundary tests passed for the wrong reason

**Phase 4. Caught before the rule shipped, by its own mirror-image test.**

`RBIPreDebitNotice` compares timestamps rather than hour indices, so that the 24-hour
pre-debit window can be probed at minute resolution instead of being rounded onto the
simulator's hourly grid. The test helper that builds a policy context defaults its
clock to `state.hour`, which is 0 unless a test says otherwise, and four of the
pre-debit tests forgot to say otherwise.

The consequence was that the rule was being evaluated as if the debit happened at the
moment of failure, so every computed lead time was **negative** — `-23.98h`,
`-24.0h`, `-2.0h`. Every one of those denials was correct by accident.

The failure that exposed it was not the denial test. It was the three *allow* tests
sitting next to it:

| Test | Expected | Got |
|---|---|---|
| notice 23h59m prior | denied | denied ✓ (for the wrong reason) |
| notice 24h01m prior | allowed | **denied** ✗ |
| notice exactly 24h prior | allowed | **denied** ✗ |
| complete, timely notice | allowed | **denied** ✗ |

**What it would have cost.** Had the boundary been tested only from the denying side —
which is the natural way to write a compliance test, because the interesting case is
the violation — all four tests would have passed, the rule would have looked verified,
and its actual behaviour at the 24-hour boundary would never have been exercised once.
A regulation test that cannot distinguish "correct" from "always denies" is not
evidence of compliance; it is evidence that something denies.

**The fix.** The four tests now pass `at_hour=48` explicitly, so the debit sits two
days after the failure and the notice's lead time is a real positive number.

**The lesson, kept.** Every threshold in the RBI suite is now tested from **both**
sides — 23h59m denied *and* 24h01m allowed, ₹15,001 denied *and* ₹14,999 allowed,
₹1,00,001 denied *and* ₹99,999 allowed. A one-sided boundary test is indistinguishable
from a rule that is stuck.

---

## 2. A development run silently truncated the committed cohort from 500 cases to 20

**Phase 5. Shipped into the working tree and caught by `git status` minutes later.**

`eval` regenerates the cohort as a side effect, so that a reviewer with a clean clone
runs one command and gets byte-identical input. That is a good property. The
implementation of it wrote `data/cohort_seed42.jsonl` unconditionally, at whatever
`--n` was passed.

So a single development run —

```
python -m recovery.cli eval --seed 42 --n 20 --arms control,baseline,agent
```

— replaced the committed **500-case** reproduction artifact with a **20-case** one. The
run itself was perfectly correct; the cohort is a pure function of `(seed, n)` and 20
cases is a valid cohort. The damage was entirely to the file on disk.

**What it would have cost.** This is the worst shape a bug can have in a submission
whose entire argument is *clone this and get the same number*.

The 20-case cohort is a strict **prefix** of the 500-case one — same generator, same
seed, `case-00000` through `case-00019`, byte-identical rows. So the truncated file is
not corrupt, not malformed, and not obviously wrong in any way a reviewer or a linter
could see. Every line in it is a real, published case. Only the line count differs.

Committed, `python -m recovery.cli repro` would have **passed**. It would have
regenerated 20 cases, compared them against the 20 cases in the file, found them
identical, and reported a clean reproduction — of a cohort that is not the one the
README quotes a number for. The headline figure and the artifact that is supposed to
prove it would have silently drifted apart, with the gate that exists to catch exactly
that reporting success.

**The fix.** `eval` now writes the cohort artifact only when there is none — a clean
clone still gets a self-contained run — and refuses to overwrite an existing one whose
case count differs, printing what it declined to do rather than doing it quietly. The
in-memory cohort for the run is unaffected, so `--n 20` still runs 20 cases.
Replacing the artifact is now the sole job of `cohort --seed 42 --n 500`.

**The lesson, kept.** A command that *reports* a result should not also *overwrite the
artifact the result is checked against.* Two regression tests hold the line: one asserts
that `--n 20` against an existing 500-case file leaves the bytes untouched and says so
on stdout, and one asserts a clean clone still gets the file written.
