"""Determinism, CRN pairing and hazard properties of the outcome model.

The first test in this file is the one that matters. If observation perturbs outcomes,
or if the draw keying depends on stream position rather than event identity, the arms
stop being comparable and every number the submission publishes becomes noise --
without anything visibly failing.
"""

from __future__ import annotations

import pytest

from recovery.cohort import generate_cohort
from recovery.declines import REASONS, DeclineClass, Method
from recovery.eval import harness
from recovery.transport.base import (
    CAUSE_SELF_HEAL,
    CHANNEL_SMS,
    STATUS_RECOVERED,
    SimParams,
)
from recovery.transport.mock import (
    MockTransport,
    fatigue_penalty,
    quiet_hours_penalty,
    self_heal_hazard,
    time_factor,
)
from tests.conftest import make_case, make_latents


class _NoOpObserverArm:
    """Takes no actions but reads every case, every hour.

    Its outcomes must be identical to the control arm's. If they are not, observation
    has side effects and the counterfactual is contaminated.
    """

    name = "noop_observer"

    def __init__(self):
        self.observations = 0

    def act(self, state, hour, transport):
        transport.observe(state.case_id, hour)
        self.observations += 1
        return []


class _ControlArm:
    name = "control"

    def act(self, state, hour, transport):
        return []


def _run(arm, cases, seed=42, params=None):
    params = params or SimParams()
    transport = MockTransport(cases, seed=seed, params=params)
    harness.drive(arm, transport, cases, params)
    return transport


# --------------------------------------------------------------------------------
# The CRN pairing test -- the important one
# --------------------------------------------------------------------------------


def test_observation_does_not_perturb_a_single_self_heal_hour():
    """Control versus an arm that observes every case every hour.

    Every case must self-heal at exactly the same hour in both. A draw keyed by stream
    position rather than event identity would fail this instantly, because the
    observing arm makes many more calls.
    """
    cases = generate_cohort(seed=42, n=200)

    control = _run(_ControlArm(), cases)
    observer_arm = _NoOpObserverArm()
    observer = _run(observer_arm, cases)

    assert observer_arm.observations > 0, "the observer arm never ran"

    control_by_case = {o.case_id: o for o in control.outcomes()}
    observer_by_case = {o.case_id: o for o in observer.outcomes()}

    assert control_by_case.keys() == observer_by_case.keys()
    for case_id, expected in control_by_case.items():
        actual = observer_by_case[case_id]
        assert actual.recovered_at_hour == expected.recovered_at_hour, case_id
        assert actual.recovered == expected.recovered, case_id
        assert actual.recovery_cause == expected.recovery_cause, case_id


def test_running_the_same_arm_twice_gives_identical_outcomes():
    cases = generate_cohort(seed=42, n=200)
    first = _run(_ControlArm(), cases).outcomes()
    second = _run(_ControlArm(), cases).outcomes()
    assert [o.to_dict() for o in first] == [o.to_dict() for o in second]


def test_arm_execution_order_does_not_change_any_arm_result(tmp_path):
    """Arms get fresh transports, so order is irrelevant -- assert it, because a
    shared transport would silently make arm two inherit arm one's history."""
    cases = generate_cohort(seed=42, n=120)

    forward = _run(_ControlArm(), cases).outcomes()
    observer_first = _run(_NoOpObserverArm(), cases).outcomes()
    reverse_control = _run(_ControlArm(), cases).outcomes()

    assert [o.to_dict() for o in forward] == [o.to_dict() for o in reverse_control]
    assert [o.recovered_at_hour for o in forward] == [
        o.recovered_at_hour for o in observer_first
    ]


def test_full_harness_run_is_reproducible(tmp_path):
    cases = generate_cohort(seed=42, n=100)
    first = harness.run(seed=42, n=100, arms=["control"], cases=cases, out_root=tmp_path)
    second = harness.run(seed=42, n=100, arms=["control"], cases=cases, out_root=tmp_path)
    assert [o.to_dict() for o in first.arms[0].outcomes] == [
        o.to_dict() for o in second.arms[0].outcomes
    ]
    # Re-running overwrites rather than appending, so the file is byte-identical too.
    assert first.arms[0].ledger_path.read_bytes() == second.arms[0].ledger_path.read_bytes()


def test_harness_ledger_verifies(tmp_path):
    result = harness.run(seed=42, n=50, arms=["control"], out_root=tmp_path)
    assert harness.verify_run(result) == {"control": True}


# --------------------------------------------------------------------------------
# Retries cannot succeed when they structurally cannot succeed
# --------------------------------------------------------------------------------


SOFT_REASONS = sorted(
    (r for r in REASONS.values() if r.klass is DeclineClass.SOFT), key=lambda r: r.code
)
NON_RETRYABLE = sorted(
    (r for r in REASONS.values() if r.klass is not DeclineClass.SOFT), key=lambda r: r.code
)


@pytest.mark.parametrize("reason", SOFT_REASONS, ids=lambda r: r.code)
def test_a_retry_inside_the_cooling_window_never_succeeds(reason):
    """It only burns a fee. This holds for every SOFT reason and every hour before
    ``min_retry_wait_h``, regardless of how willing the customer is."""
    if reason.min_retry_wait_h == 0:
        pytest.skip(f"{reason.code} has no cooling window")

    case = make_case(
        "case-cool",
        decline_reason=reason.code,
        latents=make_latents(retry_success_base=reason.retry_success_base, intent_to_pay=1.0),
    )
    transport = MockTransport([case], seed=42)

    for hour in range(reason.min_retry_wait_h):
        result = transport.attempt_retry("case-cool", hour, f"k-{hour}")
        assert result.recovered is False, f"{reason.code} recovered at hour {hour}"
        assert result.cost_paise > 0, "a doomed retry still costs money"

    assert transport.observe("case-cool", reason.min_retry_wait_h).status != STATUS_RECOVERED


@pytest.mark.parametrize("reason", NON_RETRYABLE, ids=lambda r: r.code)
def test_a_retry_on_a_hard_or_action_decline_never_succeeds(reason):
    """At any hour, at any attempt number, for a maximally willing customer.

    This is not a special case in the simulator -- ``retry_success_base`` is 0.0 for
    these reasons, so the product is identically zero by construction.
    """
    method = sorted(reason.methods, key=lambda m: m.value)[0]
    case = make_case("case-hard", decline_reason=reason.code, method=method, intent_to_pay=1.0)
    transport = MockTransport([case], seed=42)

    for hour in range(0, 336, 7):
        result = transport.attempt_retry("case-hard", hour, f"k-{hour}")
        assert result.recovered is False, f"{reason.code} recovered at hour {hour}"

    assert transport.outcomes()[0].recovered is False
    assert transport.outcomes()[0].attempts == len(range(0, 336, 7))


def test_a_well_timed_retry_on_a_soft_decline_can_succeed():
    """The complement of the two tests above: the model is not simply always zero."""
    recovered = 0
    for i in range(60):
        case = make_case(
            f"case-{i:05d}",
            decline_reason="gateway_timeout",
            latents=make_latents(retry_success_base=0.60, intent_to_pay=1.0),
        )
        transport = MockTransport([case], seed=42)
        if transport.attempt_retry(case.case_id, 24, "k").recovered:
            recovered += 1
    assert recovered > 0, "no soft decline ever recovered by retry; the model is inert"


# --------------------------------------------------------------------------------
# The self-heal hazard
# --------------------------------------------------------------------------------


def test_self_heal_hazard_integrates_to_p_self_heal_over_the_window():
    """Catches an inverted or mis-rooted hazard. ``p / W`` instead of
    ``1 - (1 - p) ** (1/W)`` would give a visibly different realised rate."""
    target = 0.35
    n = 2000
    cases = [
        make_case(f"case-{i:05d}", latents=make_latents(p_self_heal=target)) for i in range(n)
    ]
    transport = MockTransport(cases, seed=42)
    for hour in range(336):
        transport.tick(hour)

    realised = sum(1 for o in transport.outcomes() if o.recovered) / n
    assert abs(realised - target) < 0.04, f"realised {realised:.3f} vs target {target}"
    assert all(
        o.recovery_cause == CAUSE_SELF_HEAL for o in transport.outcomes() if o.recovered
    )


def test_self_heal_hazard_formula_is_the_root_not_the_quotient():
    p, w = 0.35, 336
    hazard = self_heal_hazard(p, w)
    assert abs((1 - (1 - hazard) ** w) - p) < 1e-9
    assert hazard != pytest.approx(p / w)


def test_zero_self_heal_probability_never_recovers():
    cases = [make_case(f"case-{i:05d}", latents=make_latents(p_self_heal=0.0)) for i in range(50)]
    transport = MockTransport(cases, seed=42)
    for hour in range(336):
        transport.tick(hour)
    assert not any(o.recovered for o in transport.outcomes())


def test_a_case_that_self_healed_cannot_be_recovered_again_by_a_retry():
    """Once terminal, always terminal -- and it keeps its original recovery hour."""
    case = make_case(
        "case-heal",
        decline_reason="gateway_timeout",
        latents=make_latents(p_self_heal=0.99, retry_success_base=0.60, intent_to_pay=1.0),
    )
    transport = MockTransport([case], seed=42)
    for hour in range(336):
        transport.tick(hour)

    outcome = transport.outcomes()[0]
    assert outcome.recovered and outcome.recovery_cause == CAUSE_SELF_HEAL
    healed_at = outcome.recovered_at_hour

    result = transport.attempt_retry("case-heal", 335, "k")
    assert result.ok is False and result.cost_paise == 0
    assert transport.outcomes()[0].recovered_at_hour == healed_at


# --------------------------------------------------------------------------------
# Component shapes
# --------------------------------------------------------------------------------


def test_attempt_decay_is_monotonically_decreasing():
    params = SimParams()
    values = [params.decay(n) for n in range(1, 15)]
    assert values == sorted(values, reverse=True)
    assert all(a > b for a, b in zip(values, values[1:])), values
    assert values[0] == 1.0


def test_time_factor_is_zero_before_the_cooling_window_and_positive_after():
    from recovery.declines import lookup

    reason = lookup("insufficient_funds")
    case = make_case("case-tf", decline_reason="insufficient_funds")
    assert time_factor(reason, case, reason.min_retry_wait_h - 1, 336) == 0.0
    assert time_factor(reason, case, reason.min_retry_wait_h, 336) > 0.0


def test_quiet_hours_penalise_sms_but_never_email():
    assert quiet_hours_penalty(3, CHANNEL_SMS) < 1.0
    assert quiet_hours_penalty(22, CHANNEL_SMS) < 1.0
    assert quiet_hours_penalty(14, CHANNEL_SMS) == 1.0
    for hour in range(24):
        assert quiet_hours_penalty(hour, "email") == 1.0


def test_contact_fatigue_decreases_with_each_prior_contact():
    values = [fatigue_penalty(k) for k in range(10)]
    assert values == sorted(values, reverse=True)
    assert values[0] == 1.0
    assert all(v > 0 for v in values)


# --------------------------------------------------------------------------------
# Latents never leave the simulator
# --------------------------------------------------------------------------------


def test_observe_exposes_no_latent_field():
    case = make_case("case-obs")
    transport = MockTransport([case], seed=42)
    payload = transport.observe("case-obs", 10).to_dict()
    for leak in (
        "p_self_heal",
        "retry_success_base",
        "intent_to_pay",
        "responsiveness",
        "channel_affinity",
        "latents",
    ):
        assert leak not in payload


def test_results_carry_no_probability_or_draw():
    """``detail`` must not leak the success probability: it is a function of the
    latent ``intent_to_pay`` and would reach both the arm and the ledger."""
    case = make_case("case-det", decline_reason="gateway_timeout")
    transport = MockTransport([case], seed=42)

    retry = transport.attempt_retry("case-det", 24, "k1")
    message = transport.send_message("case-det", CHANNEL_SMS, "payment_reminder", 25, "k2")
    for detail in (retry.detail, message.detail):
        assert not {"p", "probability", "draw", "u"} & set(detail)


def test_observe_never_reveals_a_future_recovery():
    case = make_case(
        "case-future",
        decline_reason="gateway_timeout",
        latents=make_latents(p_self_heal=0.99),
    )
    transport = MockTransport([case], seed=42)
    for hour in range(336):
        transport.tick(hour)
    healed_at = transport.outcomes()[0].recovered_at_hour
    assert healed_at is not None

    before = transport.observe("case-future", max(0, healed_at - 1))
    assert before.status != STATUS_RECOVERED
    assert before.recovered_at_hour is None
    assert transport.observe("case-future", healed_at).status == STATUS_RECOVERED


def test_simulator_refuses_cases_without_latents():
    from recovery.cohort import generate_cohort as gen

    stripped = [
        c.__class__(**{**{f: getattr(c, f) for f in c.__dataclass_fields__}, "latents": None})
        for c in gen(seed=42, n=3)
    ]
    with pytest.raises(ValueError, match="latents"):
        MockTransport(stripped, seed=42)


# --------------------------------------------------------------------------------
# The control arm is not inert
# --------------------------------------------------------------------------------


def test_the_control_arm_recovers_real_money_on_its_own():
    """If this is zero the measurement design is inert: there is nothing for the
    incremental subtraction to correct for, and gross would equal incremental."""
    cases = generate_cohort(seed=42, n=500)
    outcomes = _run(_ControlArm(), cases).outcomes()
    recovered_paise = sum(o.amount_paise for o in outcomes if o.recovered)
    assert recovered_paise > 0
    assert 0.05 < sum(1 for o in outcomes if o.recovered) / len(outcomes) < 0.60


def test_the_control_arm_spends_nothing():
    cases = generate_cohort(seed=42, n=200)
    outcomes = _run(_ControlArm(), cases).outcomes()
    assert sum(o.cost_paise for o in outcomes) == 0
    assert sum(o.attempts for o in outcomes) == 0
    assert sum(o.contacts for o in outcomes) == 0


def test_method_and_reason_stay_consistent_through_the_simulator():
    from recovery.declines import lookup

    cases = generate_cohort(seed=42, n=100)
    transport = MockTransport(cases, seed=42)
    for case_id in transport.case_ids():
        state = transport.observe(case_id, 0)
        assert isinstance(state.method, Method)
        assert state.method in lookup(state.decline_reason).methods
