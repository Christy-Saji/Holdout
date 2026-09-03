"""Metric arithmetic.

The two properties worth defending on video are both here: incremental recovery is
measured against the holdout, and cost-effectiveness uses that incremental denominator
rather than the flattering gross one.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from recovery.eval import metrics
from recovery.ledger import KIND_POLICY_CHECK, Ledger
from recovery.transport.mock import CaseOutcome

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
CLOCK = datetime(2026, 8, 1, 10, 0, tzinfo=IST)


def outcome(
    case_id: str,
    amount_paise: int,
    recovered: bool,
    *,
    cost_paise: int = 0,
    recovered_at_hour: int | None = None,
    attempts: int = 0,
    contacts: int = 0,
    customer_id: str | None = None,
    cause: str | None = None,
) -> CaseOutcome:
    return CaseOutcome(
        case_id=case_id,
        customer_id=customer_id or f"cust-{case_id}",
        amount_paise=amount_paise,
        recovered=recovered,
        recovered_at_hour=recovered_at_hour,
        recovery_cause=cause,
        cost_paise=cost_paise,
        attempts=attempts,
        contacts=contacts,
    )


# Three cases with known amounts and known recovery flags.
#   control recovers c1 only                       -> gross Rs 100.00
#   treatment recovers c1 and c3, spending Rs 9.00 -> gross Rs 400.00
#   incremental is therefore Rs 300.00, not Rs 400.00
CONTROL = [
    outcome("c1", 10_000, True, recovered_at_hour=120),
    outcome("c2", 20_000, False),
    outcome("c3", 30_000, False),
]
TREATMENT = [
    outcome("c1", 10_000, True, cost_paise=300, recovered_at_hour=48, attempts=1),
    outcome("c2", 20_000, False, cost_paise=300, attempts=1),
    outcome("c3", 30_000, True, cost_paise=300, recovered_at_hour=72, attempts=1),
]


# --------------------------------------------------------------------------------
# The hand-computed fixture
# --------------------------------------------------------------------------------


def test_gross_recovery_sums_recovered_amounts_only():
    assert metrics.gross_recovered_paise(CONTROL) == 10_000
    assert metrics.gross_recovered_paise(TREATMENT) == 40_000


def test_incremental_recovery_subtracts_the_holdout():
    assert metrics.incremental_recovery_paise(TREATMENT, CONTROL) == 30_000


def test_recovery_and_incremental_rates():
    assert metrics.recovery_rate(CONTROL) == pytest.approx(1 / 3)
    assert metrics.recovery_rate(TREATMENT) == pytest.approx(2 / 3)
    assert metrics.incremental_rate(TREATMENT, CONTROL) == pytest.approx(1 / 3)


def test_cost_per_incremental_rupee_uses_the_incremental_denominator():
    """Rs 9.00 spent for Rs 300.00 of incremental recovery is 0.03, not the 0.0225 a
    gross denominator would report."""
    assert metrics.total_cost_paise(TREATMENT) == 900
    assert metrics.cost_per_incremental_rupee(TREATMENT, CONTROL) == pytest.approx(900 / 30_000)
    gross_denominator = 900 / metrics.gross_recovered_paise(TREATMENT)
    assert metrics.cost_per_incremental_rupee(TREATMENT, CONTROL) != pytest.approx(
        gross_denominator
    )


# --------------------------------------------------------------------------------
# The holdout, and the undefined case
# --------------------------------------------------------------------------------


def test_the_control_arm_has_exactly_zero_incremental_recovery_against_itself():
    assert metrics.incremental_recovery_paise(CONTROL, CONTROL) == 0
    assert metrics.incremental_rate(CONTROL, CONTROL) == 0.0


def test_cost_per_incremental_rupee_is_none_when_the_delta_is_not_positive():
    """Not infinity, and not a quiet fallback to the gross denominator: a system that
    recovered nothing incremental has no cost-effectiveness to report."""
    worse = [
        outcome("c1", 10_000, False, cost_paise=5_000),
        outcome("c2", 20_000, False, cost_paise=5_000),
        outcome("c3", 30_000, False, cost_paise=5_000),
    ]
    assert metrics.incremental_recovery_paise(worse, CONTROL) < 0
    assert metrics.cost_per_incremental_rupee(worse, CONTROL) is None

    flat = [outcome(o.case_id, o.amount_paise, o.recovered, cost_paise=100) for o in CONTROL]
    assert metrics.incremental_recovery_paise(flat, CONTROL) == 0
    assert metrics.cost_per_incremental_rupee(flat, CONTROL) is None


def test_cost_per_incremental_rupee_is_never_infinity():
    result = metrics.cost_per_incremental_rupee(CONTROL, CONTROL)
    assert result is None
    assert result != float("inf")


# --------------------------------------------------------------------------------
# Money stays integer paise
# --------------------------------------------------------------------------------


def test_every_money_aggregate_is_an_int():
    for value in (
        metrics.gross_recovered_paise(TREATMENT),
        metrics.total_cost_paise(TREATMENT),
        metrics.incremental_recovery_paise(TREATMENT, CONTROL),
    ):
        assert type(value) is int


def test_rupee_formatting_happens_only_at_the_display_boundary():
    assert metrics.rupees(10_000) == "Rs 100.00"
    assert metrics.rupees(1) == "Rs 0.01"
    assert metrics.rupees(-30_000) == "-Rs 300.00"
    assert metrics.rupees(123_456_789) == "Rs 1,234,567.89"


def test_empty_cohort_does_not_divide_by_zero():
    assert metrics.recovery_rate([]) == 0.0
    assert metrics.incremental_rate([], []) == 0.0
    assert metrics.gross_recovered_paise([]) == 0


# --------------------------------------------------------------------------------
# Blocked actions come straight from the ledger
# --------------------------------------------------------------------------------


def _write_policy_ledger(path):
    with Ledger(path, run_id="run-test") as ledger:
        # allowed: every rule passed
        ledger.append(
            ts=CLOCK,
            arm="baseline",
            case_id="c1",
            kind=KIND_POLICY_CHECK,
            allowed=True,
            verdicts=[
                {"rule_id": "HardDeclineNoRetry", "allow": True, "reason": "soft", "evidence": {}},
                {"rule_id": "CoolingOff", "allow": True, "reason": "elapsed", "evidence": {}},
            ],
        )
        # denied by two rules at once -- the engine does not short-circuit, so both
        # objections are recorded and both are counted.
        ledger.append(
            ts=CLOCK + timedelta(hours=1),
            arm="baseline",
            case_id="c2",
            kind=KIND_POLICY_CHECK,
            allowed=False,
            verdicts=[
                {"rule_id": "HardDeclineNoRetry", "allow": False, "reason": "stolen", "evidence": {}},
                {"rule_id": "CoolingOff", "allow": False, "reason": "too soon", "evidence": {}},
            ],
        )
        ledger.append(
            ts=CLOCK + timedelta(hours=2),
            arm="baseline",
            case_id="c3",
            kind=KIND_POLICY_CHECK,
            allowed=False,
            verdicts=[
                {"rule_id": "HardDeclineNoRetry", "allow": False, "reason": "stolen", "evidence": {}},
                {"rule_id": "CoolingOff", "allow": True, "reason": "elapsed", "evidence": {}},
            ],
        )


def test_blocked_actions_are_counted_per_rule(tmp_path):
    path = tmp_path / "baseline.jsonl"
    _write_policy_ledger(path)
    assert metrics.blocked_actions_by_rule(path) == {"HardDeclineNoRetry": 2, "CoolingOff": 1}


def test_blocked_action_count_is_actions_not_verdicts(tmp_path):
    """Two denied actions, three objecting verdicts. The table reports actions."""
    path = tmp_path / "baseline.jsonl"
    _write_policy_ledger(path)
    assert metrics.total_blocked_actions(path) == 2


def test_an_allowed_action_contributes_no_blocked_counts(tmp_path):
    path = tmp_path / "control.jsonl"
    with Ledger(path, run_id="run-test") as ledger:
        ledger.append(
            ts=CLOCK,
            arm="control",
            case_id="c1",
            kind=KIND_POLICY_CHECK,
            allowed=True,
            verdicts=[{"rule_id": "TerminalState", "allow": True, "reason": "open", "evidence": {}}],
        )
    assert metrics.blocked_actions_by_rule(path) == {}
    assert metrics.total_blocked_actions(path) == 0


# --------------------------------------------------------------------------------
# The table
# --------------------------------------------------------------------------------


def test_the_results_table_puts_control_first(tmp_path):
    from recovery.eval import harness

    result = harness.run(seed=42, n=60, arms=["control"], out_root=tmp_path)
    computed = metrics.compute(result)
    assert computed[0].arm == "control"
    assert computed[0].incremental_paise == 0

    table = metrics.format_table(computed)
    assert table.splitlines()[2].startswith("control")


def test_compute_refuses_a_run_with_no_holdout(tmp_path):
    from recovery.eval import harness
    from recovery.eval.harness import ArmRun, RunResult
    from recovery.transport.base import SimParams

    empty = RunResult(
        run_id="run-x",
        seed=42,
        n=0,
        params=SimParams(),
        run_dir=tmp_path,
        arms=(ArmRun(arm="baseline", ledger_path=tmp_path / "baseline.jsonl", outcomes=()),),
    )
    with pytest.raises(ValueError, match="control"):
        metrics.compute(empty)
