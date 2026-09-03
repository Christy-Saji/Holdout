"""The operational rules: what may be retried, when, how often, and at what cost.

The decline-class rules are parametrised over the whole taxonomy rather than over a
hand-picked sample, so a reason added later without a class -- or with the wrong one --
fails here rather than in production.
"""

from __future__ import annotations

import pytest

from recovery.declines import REASONS, DeclineClass, UnknownDeclineReason
from recovery.policy import rules
from recovery.policy.engine import default_engine
from recovery.scoring import Scorer
from recovery.transport.base import (
    ACTION_MESSAGE,
    ACTION_RETRY,
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CHANNEL_WHATSAPP,
    STATUS_CLOSED,
    STATUS_RECOVERED,
    TEMPLATE_PRE_DEBIT_NOTICE,
    Action,
    Attempt,
    Contact,
    SimParams,
)
from tests.conftest import make_case, make_gate, make_state
from tests.policy_helpers import context_for, verdict_for

HARD = sorted((r for r in REASONS.values() if r.klass is DeclineClass.HARD), key=lambda r: r.code)
ACTIONABLE = sorted(
    (r for r in REASONS.values() if r.klass is DeclineClass.ACTION), key=lambda r: r.code
)
SOFT = sorted((r for r in REASONS.values() if r.klass is DeclineClass.SOFT), key=lambda r: r.code)


def _retry(state, hour: int = 48, key: str = "k") -> Action:
    return Action(type=ACTION_RETRY, case_id=state.case_id, at_hour=hour, idempotency_key=key)


def _message(state, *, channel=CHANNEL_SMS, hour=10, template="payment_reminder", key="k"):
    return Action(
        type=ACTION_MESSAGE,
        case_id=state.case_id,
        at_hour=hour,
        idempotency_key=key,
        channel=channel,
        template=template,
    )


# --------------------------------------------------------------------------------
# HARD declines are never retried
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", HARD, ids=lambda r: r.code)
def test_every_hard_decline_denies_a_retry(reason):
    """Retrying a stolen card burns a fee *and* trips issuer fraud heuristics,
    degrading the approval rate on the merchant's good transactions."""
    state = make_state(decline_reason=reason.code, method=sorted(reason.methods, key=lambda m: m.value)[0])
    verdict = verdict_for(rules.HardDeclineNoRetry(), _retry(state), state)
    assert verdict.allow is False
    assert verdict.evidence["klass"] == "hard"
    assert verdict.evidence["decline_reason"] == reason.code


@pytest.mark.parametrize("reason", SOFT + ACTIONABLE, ids=lambda r: r.code)
def test_the_hard_rule_does_not_object_to_a_non_hard_decline(reason):
    state = make_state(decline_reason=reason.code, method=sorted(reason.methods, key=lambda m: m.value)[0])
    assert verdict_for(rules.HardDeclineNoRetry(), _retry(state), state).allow is True


# --------------------------------------------------------------------------------
# ACTION declines are never silently retried, but may be contacted
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ACTIONABLE, ids=lambda r: r.code)
def test_every_action_decline_denies_a_retry(reason):
    """A silent retry cannot succeed by construction. The instrument needs the
    customer to change something."""
    state = make_state(decline_reason=reason.code, method=sorted(reason.methods, key=lambda m: m.value)[0])
    verdict = verdict_for(rules.ActionDeclineNoRetry(), _retry(state), state)
    assert verdict.allow is False
    assert verdict.evidence["klass"] == "action"


@pytest.mark.parametrize("reason", ACTIONABLE, ids=lambda r: r.code)
def test_every_action_decline_allows_a_contact(reason):
    """Contact is the only move that can work, so the rules must not block it."""
    state = make_state(decline_reason=reason.code, method=sorted(reason.methods, key=lambda m: m.value)[0])
    message = _message(state, hour=12)
    engine = default_engine(Scorer(params=SimParams()))
    decision = engine.evaluate(message, state, context_for(state, at_hour=12).history, state.failed_at)
    assert decision.allowed is True, [v.rule_id for v in decision.denials()]


@pytest.mark.parametrize("reason", HARD, ids=lambda r: r.code)
def test_a_hard_decline_is_not_worth_contacting_about_either(reason):
    """Nothing the customer does fixes a closed account on this instrument, so the
    budget ceiling -- expected recovery of zero -- refuses to spend on it."""
    state = make_state(decline_reason=reason.code, method=sorted(reason.methods, key=lambda m: m.value)[0])
    verdict = verdict_for(rules.BudgetCeiling(), _message(state), state)
    assert verdict.allow is False


# --------------------------------------------------------------------------------
# Cooling off
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("reason", [r for r in SOFT if r.min_retry_wait_h > 0], ids=lambda r: r.code)
def test_a_soft_decline_denies_a_retry_before_its_cooling_window(reason):
    state = make_state(decline_reason=reason.code, method=sorted(reason.methods, key=lambda m: m.value)[0])
    verdict = verdict_for(rules.CoolingOff(), _retry(state, hour=reason.min_retry_wait_h - 1), state)
    assert verdict.allow is False
    assert verdict.evidence["min_retry_wait_h"] == reason.min_retry_wait_h


@pytest.mark.parametrize("reason", SOFT, ids=lambda r: r.code)
def test_a_soft_decline_allows_a_retry_once_its_cooling_window_has_passed(reason):
    state = make_state(decline_reason=reason.code, method=sorted(reason.methods, key=lambda m: m.value)[0])
    hour = max(reason.min_retry_wait_h, rules.MIN_ATTEMPT_SPACING_H)
    assert verdict_for(rules.CoolingOff(), _retry(state, hour=hour), state).allow is True


def test_insufficient_funds_respects_the_payroll_aligned_window():
    """The largest decline bucket. A first retry waits 24 hours, and a second waits a
    further 24 -- putting it at 48h, inside the 24-72h band where Indian salary
    accounts refill. The number is the reason's own cooling window, not a constant
    invented for this rule."""
    state = make_state(decline_reason="insufficient_funds")
    assert verdict_for(rules.CoolingOff(), _retry(state, hour=23), state).allow is False
    assert verdict_for(rules.CoolingOff(), _retry(state, hour=24), state).allow is True

    after_one = make_state(
        decline_reason="insufficient_funds",
        attempts=[Attempt(hour=24, attempt_no=1, succeeded=False)],
    )
    assert verdict_for(rules.CoolingOff(), _retry(after_one, hour=47), after_one).allow is False
    verdict = verdict_for(rules.CoolingOff(), _retry(after_one, hour=48), after_one)
    assert verdict.allow is True
    assert 24 <= 48 <= 72


def test_consecutive_retries_are_never_closer_than_the_minimum_spacing():
    """Even for a reason whose cooling window is an hour: attempts an hour apart look
    like a retry storm to the issuer."""
    state = make_state(
        decline_reason="gateway_timeout",
        attempts=[Attempt(hour=2, attempt_no=1, succeeded=False)],
    )
    too_soon = verdict_for(rules.CoolingOff(), _retry(state, hour=3), state)
    assert too_soon.allow is False
    assert too_soon.evidence["required_spacing_h"] == rules.MIN_ATTEMPT_SPACING_H


# --------------------------------------------------------------------------------
# Quiet hours -- TRAI aligned
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("channel", [CHANNEL_SMS, CHANNEL_WHATSAPP])
@pytest.mark.parametrize("ist_hour", [21, 22, 23, 0, 3, 8])
def test_quiet_hours_deny_sms_and_whatsapp_between_nine_pm_and_nine_am(channel, ist_hour):
    state = make_state()
    hour = (ist_hour - state.failed_at.hour) % 24
    verdict = verdict_for(rules.QuietHours(), _message(state, channel=channel, hour=hour), state)
    assert verdict.allow is False
    assert verdict.evidence["ist_hour"] == ist_hour


@pytest.mark.parametrize("channel", [CHANNEL_SMS, CHANNEL_WHATSAPP])
@pytest.mark.parametrize("ist_hour", [9, 10, 14, 20])
def test_quiet_hours_allow_sms_and_whatsapp_during_the_day(channel, ist_hour):
    state = make_state()
    hour = (ist_hour - state.failed_at.hour) % 24
    assert verdict_for(rules.QuietHours(), _message(state, channel=channel, hour=hour), state).allow


@pytest.mark.parametrize("ist_hour", list(range(24)))
def test_quiet_hours_allow_email_at_every_hour(ist_hour):
    """Email is not covered by the TRAI-aligned window; it is read when the recipient
    chooses rather than delivered into their evening."""
    state = make_state()
    hour = (ist_hour - state.failed_at.hour) % 24
    assert verdict_for(
        rules.QuietHours(), _message(state, channel=CHANNEL_EMAIL, hour=hour), state
    ).allow is True


def test_a_regulatory_notice_is_exempt_from_quiet_hours():
    """A mandatory pre-debit notification is a transactional message, not commercial
    communication, and the debit it announces does not move to suit the window."""
    state = make_state()
    hour = (3 - state.failed_at.hour) % 24
    action = _message(state, channel=CHANNEL_SMS, hour=hour, template=TEMPLATE_PRE_DEBIT_NOTICE)
    assert verdict_for(rules.QuietHours(), action, state).allow is True


# --------------------------------------------------------------------------------
# Contact frequency
# --------------------------------------------------------------------------------


def test_contact_frequency_caps_contacts_per_customer_per_rolling_window():
    state = make_state()
    hours = [10, 40, 70]
    assert len(hours) == rules.MAX_CONTACTS_PER_WINDOW
    verdict = verdict_for(
        rules.ContactFrequency(), _message(state, hour=100), state, contact_hours=hours
    )
    assert verdict.allow is False
    assert verdict.evidence["contacts_in_window"] == rules.MAX_CONTACTS_PER_WINDOW
    assert verdict.evidence["cap"] == rules.MAX_CONTACTS_PER_WINDOW


def test_contact_frequency_allows_a_contact_below_the_cap():
    state = make_state()
    assert verdict_for(
        rules.ContactFrequency(), _message(state, hour=100), state, contact_hours=[10, 40]
    ).allow is True


def test_contacts_leave_the_rolling_window_as_it_slides():
    state = make_state()
    stale = [1, 2, 3]
    later = rules.CONTACT_WINDOW_H + 10
    assert verdict_for(
        rules.ContactFrequency(), _message(state, hour=later), state, contact_hours=stale
    ).allow is True


def test_two_contacts_cannot_land_within_a_day_of_each_other():
    state = make_state()
    verdict = verdict_for(
        rules.ContactFrequency(), _message(state, hour=20), state, contact_hours=[10]
    )
    assert verdict.allow is False
    assert verdict.evidence["hours_since_last_contact"] == 10


def test_a_regulatory_notice_does_not_consume_the_contact_budget():
    state = make_state()
    action = _message(state, hour=100, template=TEMPLATE_PRE_DEBIT_NOTICE)
    assert verdict_for(
        rules.ContactFrequency(), action, state, contact_hours=[10, 40, 70]
    ).allow is True


# --------------------------------------------------------------------------------
# Budget ceiling
# --------------------------------------------------------------------------------


def test_budget_ceiling_allows_the_action_just_below_expected_recovery():
    state = make_state(decline_reason="insufficient_funds", amount_paise=100_000)
    scorer = Scorer(params=SimParams())
    ceiling = scorer.expected_recovery_paise(state)
    assert ceiling > 300, "fixture needs headroom for at least one retry"

    action = _retry(state, hour=48)
    just_below = verdict_for(rules.BudgetCeiling(), action, state, case_spend_paise=ceiling - 300)
    assert just_below.allow is True


def test_budget_ceiling_denies_the_action_that_would_cross_expected_recovery():
    state = make_state(decline_reason="insufficient_funds", amount_paise=100_000)
    scorer = Scorer(params=SimParams())
    ceiling = scorer.expected_recovery_paise(state)

    action = _retry(state, hour=48)
    over = verdict_for(rules.BudgetCeiling(), action, state, case_spend_paise=ceiling - 299)
    assert over.allow is False
    assert over.evidence["ceiling_paise"] == ceiling
    assert over.evidence["would_spend_paise"] > ceiling


def test_recovery_that_costs_more_than_it_recovers_is_refused():
    """A tiny amount cannot justify even a single Rs 3 retry."""
    state = make_state(decline_reason="insufficient_funds", amount_paise=200)  # Rs 2.00
    assert verdict_for(rules.BudgetCeiling(), _retry(state, hour=48), state).allow is False


# --------------------------------------------------------------------------------
# Attempt cap -- derived, not a constant
# --------------------------------------------------------------------------------


def test_the_attempt_cap_is_not_a_hard_coded_number():
    """Changing the amount changes the cap, because the rule compares marginal
    expected recovery against marginal cost rather than counting to three."""
    small = _cap_for(amount_paise=5_000)  # Rs 50
    large = _cap_for(amount_paise=500_000)  # Rs 5,000
    assert large > small, f"cap did not move with amount: {small} vs {large}"


def _cap_for(amount_paise: int) -> int:
    """The first attempt number the AttemptCap rule refuses, for a soft decline."""
    rule = rules.AttemptCap()
    for attempt_no in range(1, 25):
        attempts = [
            Attempt(hour=24 * i, attempt_no=i, succeeded=False) for i in range(1, attempt_no)
        ]
        state = make_state(
            decline_reason="insufficient_funds", amount_paise=amount_paise, attempts=attempts
        )
        if not verdict_for(rule, _retry(state, hour=24 * attempt_no), state).allow:
            return attempt_no
    return 25


def test_the_attempt_cap_eventually_binds():
    assert _cap_for(amount_paise=100_000) < 25


def test_the_attempt_cap_evidence_shows_the_economics_it_compared():
    state = make_state(decline_reason="insufficient_funds", amount_paise=100_000)
    verdict = verdict_for(rules.AttemptCap(), _retry(state, hour=48), state)
    assert {"attempt_no", "p_success", "expected_recovery_paise", "marginal_cost_paise"} <= set(
        verdict.evidence
    )
    assert verdict.evidence["attempt_no"] == 1


def test_no_literal_attempt_cap_constant_exists():
    import inspect

    source = inspect.getsource(rules)
    assert "MAX_ATTEMPTS = 3" not in source
    assert "MAX_ATTEMPTS=3" not in source


# --------------------------------------------------------------------------------
# Terminal state
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("status", [STATUS_RECOVERED, STATUS_CLOSED])
def test_terminal_state_denies_a_retry(status):
    """The bug that dunts someone who already paid."""
    state = make_state(status=status, recovered_at_hour=30 if status == STATUS_RECOVERED else None)
    verdict = verdict_for(rules.TerminalState(), _retry(state, hour=60), state)
    assert verdict.allow is False
    assert verdict.evidence["status"] == status


@pytest.mark.parametrize("status", [STATUS_RECOVERED, STATUS_CLOSED])
def test_terminal_state_denies_a_message(status):
    state = make_state(status=status)
    assert verdict_for(rules.TerminalState(), _message(state), state).allow is False


def test_terminal_state_denies_every_action_type_on_a_recovered_case():
    from recovery.transport.base import ACTION_CLOSE, ACTION_TYPES

    state = make_state(status=STATUS_RECOVERED, recovered_at_hour=30)
    for action_type in sorted(ACTION_TYPES):
        action = Action(
            type=action_type,
            case_id=state.case_id,
            at_hour=60,
            idempotency_key="k",
            channel=CHANNEL_SMS if action_type == ACTION_MESSAGE else None,
        )
        assert verdict_for(rules.TerminalState(), action, state).allow is False, action_type
    assert ACTION_CLOSE in ACTION_TYPES


# --------------------------------------------------------------------------------
# Engine-level behaviour
# --------------------------------------------------------------------------------


def test_the_engine_runs_every_rule_even_when_the_first_one_denies():
    """The anti-short-circuit test. Returning on the first denial would be faster and
    would halve the value of the audit trail."""
    engine = default_engine(Scorer(params=SimParams()))
    state = make_state(status=STATUS_RECOVERED, decline_reason="stolen_card", recovered_at_hour=1)
    ctx = context_for(state, at_hour=48)

    decision = engine.evaluate(_retry(state, hour=48), state, ctx.history, ctx.clock)
    assert decision.allowed is False
    assert len(decision.verdicts) == len(engine.rules)
    assert {v.rule_id for v in decision.verdicts} == {r.rule_id for r in engine.rules}
    assert len(decision.denials()) > 1, "expected several rules to object independently"


def test_decision_allowed_is_the_conjunction_of_every_verdict():
    engine = default_engine(Scorer(params=SimParams()))
    state = make_state(decline_reason="insufficient_funds", amount_paise=100_000)
    ctx = context_for(state, at_hour=48)
    decision = engine.evaluate(_retry(state, hour=48), state, ctx.history, ctx.clock)
    assert decision.allowed == all(v.allow for v in decision.verdicts)


def test_every_verdict_carries_evidence():
    """A verdict that says 'denied' without saying what it saw is an assertion, not
    audit evidence."""
    engine = default_engine(Scorer(params=SimParams()))
    state = make_state(decline_reason="stolen_card")
    ctx = context_for(state, at_hour=48)
    decision = engine.evaluate(_retry(state, hour=48), state, ctx.history, ctx.clock)
    for verdict in decision.verdicts:
        assert isinstance(verdict.evidence, dict)
        assert verdict.reason, verdict.rule_id


def test_the_engine_does_not_swallow_an_unknown_decline_reason():
    """The policy engine must not catch UnknownDeclineReason and treat it as soft."""
    engine = default_engine(Scorer(params=SimParams()))
    state = make_state(decline_reason="insufficient_funds")
    bogus = make_state(decline_reason="insufficient_funds")
    object.__setattr__(bogus, "decline_reason", "not_a_real_code")
    ctx = context_for(state, at_hour=48)

    with pytest.raises(UnknownDeclineReason):
        engine.evaluate(_retry(bogus, hour=48), bogus, ctx.history, ctx.clock)


def test_a_denied_action_writes_a_ledger_entry_with_a_populated_rule_id(tmp_path):
    from recovery.ledger import KIND_ACTION, KIND_POLICY_CHECK, read_entries

    case = make_case("case-hard", decline_reason="stolen_card")
    gate = make_gate(tmp_path, [case])
    state = gate.transport.observe("case-hard", 48)

    decision, result = gate.execute(_retry(state, hour=48), state)
    gate.ledger.close()

    assert decision.allowed is False and result is None
    rows = list(read_entries(gate.ledger.path))
    checks = [r for r in rows if r["kind"] == KIND_POLICY_CHECK]
    assert len(checks) == 1
    assert checks[0]["allowed"] is False
    denied = [v for v in checks[0]["verdicts"] if not v["allow"]]
    assert denied and all(v["rule_id"] for v in denied)
    assert not [r for r in rows if r["kind"] == KIND_ACTION]


def test_a_denied_action_never_reaches_the_transport(tmp_path):
    case = make_case("case-hard2", decline_reason="stolen_card")
    gate = make_gate(tmp_path, [case])
    state = gate.transport.observe("case-hard2", 48)

    gate.execute(_retry(state, hour=48), state)
    gate.ledger.close()
    assert gate.transport.outcomes()[0].attempts == 0
    assert gate.transport.outcomes()[0].cost_paise == 0


def test_an_allowed_action_reaches_the_transport_and_is_logged(tmp_path):
    from recovery.ledger import KIND_ACTION, KIND_POLICY_CHECK, read_entries

    case = make_case("case-soft", decline_reason="insufficient_funds", amount_paise=200_000)
    gate = make_gate(tmp_path, [case])
    state = gate.transport.observe("case-soft", 48)

    decision, result = gate.execute(_retry(state, hour=48), state)
    gate.ledger.close()

    assert decision.allowed is True, [v.rule_id for v in decision.denials()]
    assert result is not None
    assert gate.transport.outcomes()[0].attempts == 1

    rows = list(read_entries(gate.ledger.path))
    assert [r["kind"] for r in rows] == [KIND_POLICY_CHECK, KIND_ACTION]
    assert rows[1]["cost_paise"] == 300


def test_a_dry_run_preview_writes_nothing_and_touches_nothing(tmp_path):
    from recovery.ledger import read_entries

    case = make_case("case-preview", decline_reason="insufficient_funds", amount_paise=200_000)
    gate = make_gate(tmp_path, [case])
    state = gate.transport.observe("case-preview", 48)

    decision = gate.preview(_retry(state, hour=48), state)
    gate.ledger.close()

    assert decision.allowed is True
    assert list(read_entries(gate.ledger.path)) == []
    assert gate.transport.outcomes()[0].attempts == 0
    assert gate.transport.outcomes()[0].cost_paise == 0
