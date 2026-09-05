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


# -- time-to-cash (phase 6) -----------------------------------------------------------


def test_time_to_cash_averages_only_the_cases_that_recovered():
    """The bias trap: a non-recovery scored as day zero drags the mean down.

    Two recoveries at 24h and 72h average two days. Counting the third, unrecovered
    case as a zero would report 1.33 days and would make the arm that recovers *less*
    look faster -- a large silent bias, and the pitfall phase 6 names by name.
    """
    outcomes = [
        outcome("a", 10_000, True, recovered_at_hour=24),
        outcome("b", 10_000, True, recovered_at_hour=72),
        outcome("c", 10_000, False),
    ]
    assert metrics.time_to_cash_days(outcomes) == pytest.approx(2.0)


def test_time_to_cash_is_none_not_zero_when_nothing_recovered():
    outcomes = [outcome("a", 10_000, False), outcome("b", 10_000, False)]
    assert metrics.time_to_cash_days(outcomes) is None


def test_time_to_cash_is_none_for_an_empty_arm():
    assert metrics.time_to_cash_days([]) is None


# -- contact fatigue (phase 6) --------------------------------------------------------


def test_contact_fatigue_p95_matches_the_hand_computed_value():
    """Twenty customers contacted 1..20 times. Nearest-rank P95 is the 19th value."""
    outcomes = [
        outcome(f"case-{i}", 10_000, False, contacts=i, customer_id=f"cust-{i}")
        for i in range(1, 21)
    ]
    fatigue = metrics.contact_fatigue(outcomes)

    assert fatigue.n_customers == 20
    assert fatigue.mean == pytest.approx(10.5)
    assert fatigue.p95 == 19


def test_contact_fatigue_aggregates_per_customer_not_per_case():
    """One customer with two failed cases feels the sum of both, not the average."""
    outcomes = [
        outcome("case-1", 10_000, False, contacts=3, customer_id="cust-shared"),
        outcome("case-2", 10_000, False, contacts=4, customer_id="cust-shared"),
    ]
    fatigue = metrics.contact_fatigue(outcomes)

    assert fatigue.n_customers == 1
    assert fatigue.mean == pytest.approx(7.0)
    assert fatigue.p95 == 7


def test_contact_fatigue_on_an_empty_arm_is_zero_not_an_error():
    fatigue = metrics.contact_fatigue([])
    assert (fatigue.mean, fatigue.p95, fatigue.n_customers) == (0.0, 0, 0)


def test_bootstrap_replicate_count_matches_the_published_spec():
    """B = 2000 is specified in the build plan §7 and quoted in the README."""
    assert metrics.BOOTSTRAP_REPLICATES == 2000


# -- the pairing test (phase 6 exit criterion) ----------------------------------------


def _unpaired_interval(arm, control, *, seed: int = 42, replicates: int = 2000):
    """A deliberately wrong bootstrap: each arm resampled independently.

    This is the version written from memory. It runs without error and silently throws
    away the pairing that common random numbers bought, so it exists here only as the
    thing the paired estimator has to beat.
    """
    import random

    rng = random.Random(seed)
    arm_paise = [o.recovered_paise for o in arm]
    control_paise = [o.recovered_paise for o in control]
    n = len(arm_paise)

    deltas = sorted(
        sum(rng.choices(arm_paise, k=n)) - sum(rng.choices(control_paise, k=n))
        for _ in range(replicates)
    )
    lo = deltas[int(round(0.025 * (replicates - 1)))]
    hi = deltas[int(round(0.975 * (replicates - 1)))]
    return lo, hi


def test_paired_bootstrap_is_strictly_narrower_than_the_unpaired_one():
    """The test that proves the CRN design is actually paying off.

    The arms share a cohort and hugely variable amounts, so the *gross* total of either
    arm is noisy while their *difference* is not: the arm recovers everything the
    control does, plus one case in five. A paired resample sees only that difference. An
    unpaired one sees both totals' noise and reports an interval several times too wide.

    If these two intervals ever come out the same width, the pairing is not being used
    and every published interval in this project is wrong.
    """
    control, arm = [], []
    for i in range(200):
        amount = 10_000 + (i % 37) * 250_000  # wide spread: the noise the pairing kills
        control.append(outcome(f"case-{i}", amount, i % 5 == 0, recovered_at_hour=48))
        arm.append(outcome(f"case-{i}", amount, i % 5 in (0, 1), recovered_at_hour=48))

    paired = metrics.bootstrap_incremental(arm, control, seed=42)
    lo, hi = _unpaired_interval(arm, control, seed=42)

    assert (hi - lo) > 0
    assert (paired.hi - paired.lo) < (hi - lo)
