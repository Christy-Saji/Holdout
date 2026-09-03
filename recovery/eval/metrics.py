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
further down this module.
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
        f"{'rate':>7} {'cost':>12} {'cost/incr Rs':>13} {'blocked':>8}"
    )
    lines = [header, "-" * len(header)]
    for m in rows:
        cpi = "n/a" if m.cost_per_incremental_rupee is None else f"{m.cost_per_incremental_rupee:.3f}"
        lines.append(
            f"{m.arm:<10} {m.n_recovered:>4}/{m.n:<5} {rupees(m.gross_paise):>16} "
            f"{rupees(m.incremental_paise):>16} {m.recovery_rate:>6.1%} "
            f"{rupees(m.cost_paise):>12} {cpi:>13} {m.blocked_actions:>8}"
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
