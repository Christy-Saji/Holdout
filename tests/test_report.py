"""Phase 6: bootstrap intervals, the report layer, and the sensitivity sweep.

The interval is the part of this project that stops the headline being a single
number stated without error bars -- which is the same class of mistake as reporting
gross recovery instead of incremental, one level up. So the properties defended here
are that the interval is *paired* (the CRN design's variance reduction is not thrown
away), *deterministic* (it reproduces from the seed like everything else), and
*honest at the boundaries* (a degenerate holdout says so; a straddling interval says
so).
"""

from __future__ import annotations

import pytest

from recovery.eval import metrics, report
from recovery.transport.mock import CaseOutcome


def outcome(case_id: str, amount_paise: int, recovered: bool, *, cost_paise: int = 0) -> CaseOutcome:
    return CaseOutcome(
        case_id=case_id,
        customer_id=f"cust-{case_id}",
        amount_paise=amount_paise,
        recovered=recovered,
        recovered_at_hour=48 if recovered else None,
        recovery_cause="retry" if recovered else None,
        cost_paise=cost_paise,
        attempts=1 if cost_paise else 0,
        contacts=0,
    )


def cohort(flags: list[bool], *, amount: int = 10_000, cost: int = 0) -> list[CaseOutcome]:
    return [
        outcome(f"c{i}", amount, flag, cost_paise=cost) for i, flag in enumerate(flags)
    ]


CONTROL = cohort([True, False, False, False, False, False, False, False])
TREATMENT = cohort([True, True, True, True, False, False, False, False], cost=300)


# --------------------------------------------------------------------------------
# The bootstrap interval
# --------------------------------------------------------------------------------


def test_the_holdout_has_a_degenerate_interval_against_itself():
    """Every resampled delta is a sum of zeroes, so the interval collapses onto zero.

    Kept rather than special-cased: a control row reporting a non-zero interval would
    mean the pairing had come apart somewhere upstream.
    """
    ci = metrics.bootstrap_incremental(CONTROL, CONTROL, seed=42, replicates=200)
    assert (ci.point, ci.lo, ci.hi) == (0, 0, 0)
    assert ci.excludes_zero is False


def test_the_interval_brackets_the_point_estimate():
    ci = metrics.bootstrap_incremental(TREATMENT, CONTROL, seed=42, replicates=500)
    assert ci.lo <= ci.point <= ci.hi
    assert ci.point == metrics.incremental_recovery_paise(TREATMENT, CONTROL)


def test_the_bootstrap_reproduces_exactly_from_the_seed():
    a = metrics.bootstrap_incremental(TREATMENT, CONTROL, seed=42, replicates=300)
    b = metrics.bootstrap_incremental(TREATMENT, CONTROL, seed=42, replicates=300)
    assert a == b


def test_a_different_seed_gives_a_different_resampling():
    a = metrics.bootstrap_incremental(TREATMENT, CONTROL, seed=42, replicates=300)
    b = metrics.bootstrap_incremental(TREATMENT, CONTROL, seed=7, replicates=300)
    assert (a.lo, a.hi) != (b.lo, b.hi)
    assert a.point == b.point  # the point estimate is not a random quantity


def test_the_bootstrap_refuses_arms_that_are_not_paired():
    """Resampling unpaired arms would silently discard the CRN variance reduction."""
    with pytest.raises(ValueError, match="not paired"):
        metrics.bootstrap_incremental(TREATMENT, CONTROL[:3], seed=42, replicates=10)


def test_interval_bounds_are_integer_paise():
    ci = metrics.bootstrap_incremental(TREATMENT, CONTROL, seed=42, replicates=200)
    assert isinstance(ci.lo, int) and isinstance(ci.hi, int) and isinstance(ci.point, int)


def test_excludes_zero_is_false_when_the_interval_straddles_zero():
    straddling = metrics.ConfidenceInterval(point=100, lo=-50, hi=250, level=0.95, replicates=10)
    assert straddling.excludes_zero is False
    clear = metrics.ConfidenceInterval(point=100, lo=20, hi=250, level=0.95, replicates=10)
    assert clear.excludes_zero is True
    negative = metrics.ConfidenceInterval(point=-100, lo=-250, hi=-20, level=0.95, replicates=10)
    assert negative.excludes_zero is True


def test_an_empty_cohort_yields_a_zero_interval_rather_than_dividing_by_zero():
    ci = metrics.bootstrap_incremental([], [], seed=42, replicates=10)
    assert (ci.point, ci.lo, ci.hi) == (0, 0, 0)


def test_a_wider_level_gives_a_wider_interval():
    narrow = metrics.bootstrap_incremental(TREATMENT, CONTROL, seed=42, replicates=500, level=0.50)
    wide = metrics.bootstrap_incremental(TREATMENT, CONTROL, seed=42, replicates=500, level=0.99)
    assert (wide.hi - wide.lo) >= (narrow.hi - narrow.lo)


def test_the_interval_table_marks_the_holdout_rather_than_failing_it():
    """A '-' on the control row, not a 'NO' that would read as a failed result."""
    table = metrics.format_intervals(
        {
            "control": metrics.ConfidenceInterval(0, 0, 0, 0.95, 10),
            "baseline": metrics.ConfidenceInterval(100, 20, 250, 0.95, 10),
        }
    )
    control_row = next(line for line in table.splitlines() if line.startswith("control"))
    baseline_row = next(line for line in table.splitlines() if line.startswith("baseline"))
    assert control_row.rstrip().endswith("-")
    assert baseline_row.rstrip().endswith("yes")


# --------------------------------------------------------------------------------
# Locating a run
# --------------------------------------------------------------------------------


def test_a_missing_run_says_how_to_produce_one(tmp_path):
    with pytest.raises(SystemExit, match="recovery.cli eval"):
        report.resolve_run_dir(None, tmp_path)


def test_a_named_run_that_does_not_exist_names_the_path(tmp_path):
    with pytest.raises(SystemExit, match="manifest.json"):
        report.resolve_run_dir("run-nope", tmp_path)


def test_several_runs_ask_which_one(tmp_path):
    for name in ("run-a", "run-b"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="--run-id"):
        report.resolve_run_dir(None, tmp_path)


def test_a_single_run_is_found_without_being_named(tmp_path):
    (tmp_path / "run-a").mkdir()
    (tmp_path / "run-a" / "manifest.json").write_text("{}", encoding="utf-8")
    assert report.resolve_run_dir(None, tmp_path).name == "run-a"


# --------------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------------


def base_params():
    from recovery.transport.base import SimParams

    return SimParams()


def test_a_multiplier_of_one_is_the_identity():
    """The published run must sit exactly on the x1.0 point of every sweep chart."""
    base = base_params()
    for target in ("p_self_heal_scale", "costs.retry_paise", "attempt_decay"):
        assert report.apply_multiplier(base, target, 1.0).to_dict() == base.to_dict()


def test_scaling_the_self_heal_rate_scales_only_that_field():
    base = base_params()
    scaled = report.apply_multiplier(base, "p_self_heal_scale", 1.5)
    assert scaled.p_self_heal_scale == pytest.approx(1.5)
    assert scaled.costs.retry_paise == base.costs.retry_paise
    assert scaled.attempt_decay == base.attempt_decay


def test_a_scaled_cost_stays_integer_paise():
    scaled = report.apply_multiplier(base_params(), "costs.retry_paise", 1.5)
    assert scaled.costs.retry_paise == 450
    assert isinstance(scaled.costs.retry_paise, int)


def test_a_scaled_decay_curve_stays_monotone_and_bounded():
    scaled = report.apply_multiplier(base_params(), "attempt_decay", 2.0)
    assert all(a >= b for a, b in zip(scaled.attempt_decay, scaled.attempt_decay[1:]))
    assert max(scaled.attempt_decay) <= 1.0


def test_an_unknown_parameter_lists_the_ones_that_work():
    with pytest.raises(SystemExit, match="p_self_heal"):
        report.sweep_main("nonsense", [0.5, 1.5])


def test_the_agent_arm_cannot_be_swept():
    """Every sweep point changes the tool results, so every point is a cassette miss."""
    with pytest.raises(SystemExit, match="cassette miss"):
        report.sweep_main("p_self_heal", [0.5, 1.5], arms=["control", "agent"])


def test_an_inverted_range_is_refused():
    with pytest.raises(SystemExit, match="lo < hi"):
        report.sweep_main("p_self_heal", [1.5, 0.5])


def test_a_sweep_needs_at_least_two_points():
    with pytest.raises(SystemExit, match="at least 2"):
        report.sweep_main("p_self_heal", [0.5, 1.5], points=1)


def test_a_sweep_needs_a_treatment_arm_beside_the_holdout():
    with pytest.raises(SystemExit, match="treatment arm"):
        report.sweep_main("p_self_heal", [0.5, 1.5], arms=["control"])


def sweep_point(multiplier: float, incremental: int, lo: int, hi: int) -> report.SweepPoint:
    return report.SweepPoint(
        multiplier=multiplier,
        value=multiplier,
        incremental_paise=incremental,
        ci=metrics.ConfidenceInterval(incremental, lo, hi, 0.95, 100),
        cost_per_incremental_rupee=0.02 if incremental > 0 else None,
    )


def test_a_surviving_sign_is_reported_as_surviving():
    table = report.format_sweep(
        [sweep_point(0.5, 9000, 7000, 11000), sweep_point(1.5, 6000, 4500, 8000)],
        param="p_self_heal",
    )
    assert "sign survives" in table
    assert "the interval excludes zero at 2/2 points." in table


def test_a_sign_that_flips_is_reported_loudly_and_named_a_limitation():
    """The whole competition screens for this being reported rather than tuned away."""
    table = report.format_sweep(
        [sweep_point(0.5, 9000, 7000, 11000), sweep_point(1.5, -500, -3000, 2000)],
        param="p_self_heal",
    )
    assert "SIGN DOES NOT SURVIVE" in table
    assert "x1.5" in table
    assert "stated limitation" in table
    assert "the interval excludes zero at 1/2 points." in table
