"""Evaluation metrics.

The headline is **incremental** recovery, not gross. Gross recovery is the number
everyone else reports and it is inflated by every case that would have recovered on
its own -- which, under this project's own generative model, is a large minority of
them. Subtracting the control arm is what turns a marketing number into a measurement.

Two arithmetic commitments that are easy to get quietly wrong:

- **Cost per incremental rupee has an incremental denominator.** Dividing cost by
  *gross* recovery flatters the result and is the same error as reporting gross
  recovery in the first place. When the incremental delta is zero or negative the
  ratio is undefined -- this module returns ``None``, never infinity, and never
  silently falls back to the gross denominator.
- **Money stays integer paise through every aggregation.** Rupees appear only in
  formatted output, at the display boundary.

Bootstrap confidence intervals, time-to-cash and contact fatigue are phase 6 and live
further down this module. Two of those three carry their own bias trap, documented at
the function: time-to-cash must not score a non-recovery as day zero, and contact
fatigue aggregates per customer rather than per case.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from recovery.ledger import KIND_POLICY_CHECK, read_entries
from recovery.transport.mock import CaseOutcome

Outcomes = Sequence[CaseOutcome]


# -- primitives ---------------------------------------------------------------------


def gross_recovered_paise(outcomes: Outcomes) -> int:
    """``R_a = sum(amount_i * 1[recovered_i])``. Integer paise."""
    return sum(o.amount_paise for o in outcomes if o.recovered)


def n_recovered(outcomes: Outcomes) -> int:
    return sum(1 for o in outcomes if o.recovered)


def total_cost_paise(outcomes: Outcomes) -> int:
    return sum(o.cost_paise for o in outcomes)


def recovery_rate(outcomes: Outcomes) -> float:
    return n_recovered(outcomes) / len(outcomes) if outcomes else 0.0


def incremental_recovery_paise(arm: Outcomes, control: Outcomes) -> int:
    """``delta_a = R_a - R_control``. The headline number."""
    return gross_recovered_paise(arm) - gross_recovered_paise(control)


def incremental_rate(arm: Outcomes, control: Outcomes) -> float:
    if not arm:
        return 0.0
    return (n_recovered(arm) - n_recovered(control)) / len(arm)


def cost_per_incremental_rupee(arm: Outcomes, control: Outcomes) -> float | None:
    """``C_a / delta_a``, or ``None`` when the delta is not positive.

    Both numerator and denominator are paise, so the ratio is rupees of cost per
    rupee of *incremental* recovery. Undefined is reported as undefined: a system that
    recovered nothing incremental has no meaningful cost-effectiveness, and returning
    infinity or quietly switching to a gross denominator would hide that.
    """
    delta = incremental_recovery_paise(arm, control)
    if delta <= 0:
        return None
    return total_cost_paise(arm) / delta


def blocked_actions_by_rule(ledger_path: Path | str) -> dict[str, int]:
    """Denial counts per ``rule_id``, straight from the ledger.

    This is the blocked-action log: what the system *refused to do*. Because the
    policy engine never short-circuits, a single denied action contributes one count
    for every rule that objected to it, not just the first.
    """
    counts: Counter[str] = Counter()
    for row in read_entries(ledger_path):
        if row["kind"] != KIND_POLICY_CHECK or row.get("allowed") is not False:
            continue
        for verdict in row.get("verdicts") or []:
            if not verdict.get("allow"):
                counts[verdict["rule_id"]] += 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def total_blocked_actions(ledger_path: Path | str) -> int:
    """Number of *actions* denied, as opposed to the number of objecting verdicts."""
    return sum(
        1
        for row in read_entries(ledger_path)
        if row["kind"] == KIND_POLICY_CHECK and row.get("allowed") is False
    )


# -- time-to-cash and contact fatigue -------------------------------------------------

HOURS_PER_DAY = 24


def time_to_cash_days(outcomes: Outcomes) -> float | None:
    """Mean days from failure to recovery, over the cases that actually recovered.

    A case that never recovered has no time-to-cash, and scoring it as zero would
    assert it recovered instantly -- a large silent bias toward whichever arm recovers
    *less*, since every non-recovery would drag that arm's mean down toward day zero.
    Non-recoveries are therefore excluded from the mean rather than counted as zero,
    and an arm that recovered nothing at all returns ``None``.

    The metric earns its place even when two arms recover the same money: the same
    rupee arriving nine days earlier is worth something in working capital.
    """
    hours = [
        o.recovered_at_hour
        for o in outcomes
        if o.recovered and o.recovered_at_hour is not None
    ]
    if not hours:
        return None
    return sum(hours) / len(hours) / HOURS_PER_DAY


@dataclass(frozen=True)
class ContactFatigue:
    """How hard an arm leans on its customers. Aggregated per *customer*, not per case.

    One customer can own more than one failed case, and it is the customer who
    experiences the sum of the messages, not the case. Aggregating per case would
    understate the worst-hit customers, which is precisely who this metric exists to
    find -- the P95 is the number that says whether the tail is being harassed.
    """

    mean: float
    p95: int
    n_customers: int


def contact_fatigue(outcomes: Outcomes) -> ContactFatigue:
    per_customer: Counter[str] = Counter()
    for o in outcomes:
        per_customer[o.customer_id] += o.contacts
    if not per_customer:
        return ContactFatigue(0.0, 0, 0)
    counts = sorted(per_customer.values())
    return ContactFatigue(
        mean=sum(counts) / len(counts),
        p95=_percentile(counts, 0.95),
        n_customers=len(counts),
    )


# -- per-arm summary ------------------------------------------------------------------


@dataclass(frozen=True)
class ArmMetrics:
    arm: str
    n: int
    n_recovered: int
    gross_paise: int
    incremental_paise: int
    recovery_rate: float
    incremental_rate: float
    cost_paise: int
    cost_per_incremental_rupee: float | None
    attempts: int
    contacts: int
    time_to_cash_days: float | None
    customers: int
    contacts_mean: float
    contacts_p95: int
    blocked_actions: int
    blocked_by_rule: dict[str, int]

    @property
    def is_control(self) -> bool:
        return self.incremental_paise == 0 and self.cost_paise == 0


def compute_arm_metrics(
    arm: str,
    outcomes: Outcomes,
    control: Outcomes,
    ledger_path: Path | str | None = None,
) -> ArmMetrics:
    blocked_by_rule = blocked_actions_by_rule(ledger_path) if ledger_path else {}
    blocked = total_blocked_actions(ledger_path) if ledger_path else 0
    fatigue = contact_fatigue(outcomes)
    return ArmMetrics(
        arm=arm,
        n=len(outcomes),
        n_recovered=n_recovered(outcomes),
        gross_paise=gross_recovered_paise(outcomes),
        incremental_paise=incremental_recovery_paise(outcomes, control),
        recovery_rate=recovery_rate(outcomes),
        incremental_rate=incremental_rate(outcomes, control),
        cost_paise=total_cost_paise(outcomes),
        cost_per_incremental_rupee=cost_per_incremental_rupee(outcomes, control),
        attempts=sum(o.attempts for o in outcomes),
        contacts=sum(o.contacts for o in outcomes),
        time_to_cash_days=time_to_cash_days(outcomes),
        customers=fatigue.n_customers,
        contacts_mean=fatigue.mean,
        contacts_p95=fatigue.p95,
        blocked_actions=blocked,
        blocked_by_rule=blocked_by_rule,
    )


def compute(run, control_arm: str = "control") -> list[ArmMetrics]:
    """Metrics for every arm in a run, **control first**.

    Control leads because every other row is read as a difference from it.
    """
    by_arm = run.by_arm
    if control_arm not in by_arm:
        raise ValueError(
            f"no {control_arm!r} arm in this run ({sorted(by_arm)}); "
            "incremental recovery is undefined without the holdout"
        )
    control = by_arm[control_arm].outcomes

    ordered = [control_arm] + [a for a in by_arm if a != control_arm]
    return [
        compute_arm_metrics(name, by_arm[name].outcomes, control, by_arm[name].ledger_path)
        for name in ordered
    ]


# -- display ----------------------------------------------------------------------------


def rupees(paise: int) -> str:
    """Paise to a rupee string. The **only** place the conversion happens."""
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    return f"{sign}Rs {whole:,}.{frac:02d}"


def format_table(metrics: Iterable[ArmMetrics]) -> str:
    """The results table. Control row first, since every other row is a difference."""
    rows = list(metrics)
    header = (
        f"{'arm':<10} {'recovered':>10} {'gross':>16} {'incremental':>16} "
        f"{'rate':>7} {'cost':>12} {'cost/incr Rs':>13} {'days to cash':>13} {'blocked':>8}"
    )
    lines = [header, "-" * len(header)]
    for m in rows:
        cpi = "n/a" if m.cost_per_incremental_rupee is None else f"{m.cost_per_incremental_rupee:.3f}"
        ttc = "n/a" if m.time_to_cash_days is None else f"{m.time_to_cash_days:.2f}"
        lines.append(
            f"{m.arm:<10} {m.n_recovered:>4}/{m.n:<5} {rupees(m.gross_paise):>16} "
            f"{rupees(m.incremental_paise):>16} {m.recovery_rate:>6.1%} "
            f"{rupees(m.cost_paise):>12} {cpi:>13} {ttc:>13} {m.blocked_actions:>8}"
        )
    return "\n".join(lines)


def format_fatigue(metrics: Iterable[ArmMetrics]) -> str:
    """Contacts per customer, mean and P95. The tail is the half that matters."""
    rows = list(metrics)
    if not rows:
        return "no contact fatigue to report"
    header = f"{'arm':<10} {'customers':>10} {'mean contacts':>14} {'P95 contacts':>13}"
    lines = [header, "-" * len(header)]
    for m in rows:
        lines.append(
            f"{m.arm:<10} {m.customers:>10} {m.contacts_mean:>14.2f} {m.contacts_p95:>13}"
        )
    return "\n".join(lines)


def format_blocked(metrics: Iterable[ArmMetrics]) -> str:
    """Blocked actions grouped by rule. The audit trail's interesting half."""
    lines: list[str] = []
    for m in metrics:
        if not m.blocked_by_rule:
            continue
        lines.append(f"{m.arm}: {m.blocked_actions} action(s) denied")
        for rule_id, count in m.blocked_by_rule.items():
            lines.append(f"    {rule_id:<28} {count:>6}")
    return "\n".join(lines) if lines else "no blocked actions"


# -- bootstrap confidence intervals (phase 6) -----------------------------------------

BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_LEVEL = 0.95


@dataclass(frozen=True)
class ConfidenceInterval:
    """A percentile bootstrap interval on an integer-paise quantity."""

    point: int
    lo: int
    hi: int
    level: float
    replicates: int

    @property
    def excludes_zero(self) -> bool:
        """Whether the interval sits entirely on one side of zero.

        This is the question the headline number actually has to answer. An
        incremental recovery of Rs 82,931 whose interval straddles zero is a number
        that has not been measured, it has only been computed.
        """
        return (self.lo > 0 and self.hi > 0) or (self.lo < 0 and self.hi < 0)

    def to_dict(self) -> dict:
        return {
            "point": self.point,
            "lo": self.lo,
            "hi": self.hi,
            "level": self.level,
            "replicates": self.replicates,
            "excludes_zero": self.excludes_zero,
        }


def _paired(arm: Outcomes, control: Outcomes) -> list[tuple[CaseOutcome, CaseOutcome]]:
    """Align an arm's outcomes with the control's, by ``case_id``.

    The arms are paired by construction -- same cohort, same latents, same common
    random numbers -- so the resampling unit is the *case*, carrying both arms'
    outcomes with it. Resampling each arm independently would throw away exactly the
    variance reduction the CRN design was built to buy, and would report an interval
    several times too wide.
    """
    by_case = {o.case_id: o for o in control}
    missing = sorted(o.case_id for o in arm if o.case_id not in by_case)
    if missing:
        raise ValueError(
            f"{len(missing)} case(s) in the arm have no control counterpart "
            f"(first: {missing[0]}); the arms are not paired"
        )
    return [(o, by_case[o.case_id]) for o in arm]


def _percentile(sorted_values: Sequence[int], q: float) -> int:
    """Nearest-rank percentile on an already-sorted sequence."""
    if not sorted_values:
        raise ValueError("no replicates to take a percentile of")
    rank = max(0, min(len(sorted_values) - 1, int(round(q * (len(sorted_values) - 1)))))
    return sorted_values[rank]


def bootstrap_incremental(
    arm: Outcomes,
    control: Outcomes,
    *,
    seed: int = 42,
    replicates: int = BOOTSTRAP_REPLICATES,
    level: float = BOOTSTRAP_LEVEL,
) -> ConfidenceInterval:
    """Percentile bootstrap interval on incremental recovery, in integer paise.

    **Paired, and drawn through the project's CRN helper.** Cases are resampled with
    replacement; each draw carries both the arm's outcome and the control's for that
    same case. Every index comes from :func:`recovery.cohort.u`, so the interval is a
    pure function of ``(seed, replicates, outcomes)`` and reproduces exactly on any
    machine -- the same standard the rest of the project holds itself to. A sequential
    RNG here would make the published interval unreproducible for the sake of nothing.
    """
    from recovery.cohort import u

    pairs = _paired(arm, control)
    n = len(pairs)
    point = incremental_recovery_paise(arm, control)
    if n == 0:
        return ConfidenceInterval(0, 0, 0, level, replicates)

    deltas = [a.recovered_paise - c.recovered_paise for a, c in pairs]

    resampled: list[int] = []
    for b in range(replicates):
        key = f"boot-{b}"
        total = 0
        for i in range(n):
            total += deltas[int(u(seed, key, "bootstrap_index", i) * n)]
        resampled.append(total)

    resampled.sort()
    tail = (1.0 - level) / 2.0
    return ConfidenceInterval(
        point=point,
        lo=_percentile(resampled, tail),
        hi=_percentile(resampled, 1.0 - tail),
        level=level,
        replicates=replicates,
    )


def confidence_intervals(
    run,
    control_arm: str = "control",
    *,
    seed: int | None = None,
    replicates: int = BOOTSTRAP_REPLICATES,
    level: float = BOOTSTRAP_LEVEL,
) -> dict[str, ConfidenceInterval]:
    """One interval per arm in the run, keyed by arm name. Control included.

    The control arm's interval against itself is exactly ``(0, 0, 0)`` -- every
    resampled delta is a sum of zeroes. That is worth keeping rather than special
    casing: a control row that reported a non-zero interval would mean the pairing
    had come apart.
    """
    by_arm = run.by_arm
    if control_arm not in by_arm:
        raise ValueError(
            f"no {control_arm!r} arm in this run ({sorted(by_arm)}); "
            "a confidence interval on incremental recovery is undefined without it"
        )
    control = by_arm[control_arm].outcomes
    boot_seed = run.seed if seed is None else seed
    return {
        name: bootstrap_incremental(
            by_arm[name].outcomes,
            control,
            seed=boot_seed,
            replicates=replicates,
            level=level,
        )
        for name in [control_arm] + [a for a in by_arm if a != control_arm]
    }


def format_intervals(intervals: dict[str, ConfidenceInterval]) -> str:
    """The interval table. Read alongside the results table, never apart from it."""
    if not intervals:
        return "no confidence intervals"
    first = next(iter(intervals.values()))
    header = (
        f"{'arm':<10} {'incremental':>16} "
        f"{int(first.level * 100)}% interval{'':>14} {'excludes 0':>11}"
    )
    lines = [header, "-" * len(header)]
    for arm, ci in intervals.items():
        span = f"[{rupees(ci.lo)}, {rupees(ci.hi)}]"
        # The holdout's interval against itself is degenerate by construction, so it
        # gets a dash rather than a "no" that would read as a failed result.
        degenerate = ci.point == 0 and ci.lo == 0 and ci.hi == 0
        verdict = "-" if degenerate else ("yes" if ci.excludes_zero else "NO")
        lines.append(f"{arm:<10} {rupees(ci.point):>16} {span:>28} {verdict:>11}")
    return "\n".join(lines)
