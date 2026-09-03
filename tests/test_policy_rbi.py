"""RBI Digital Payments -- E-mandate Framework, 2026. These tests are the evidence.

Notified 21 April 2026, effective immediately. It consolidates the prior e-mandate
circulars and makes **acquirers responsible for their merchants' compliance**, which
is why a payments processor cares about a rule that nominally binds its customers.

Read the test names in order and you have read the parts of the regulation this system
encodes. The one that matters most is
``test_afa_required_on_first_debit_regardless_of_amount``: the first-debit requirement
is the part of the framework most summaries drop, and it is not an amount rule at all.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from recovery.declines import Method
from recovery.policy.rbi import (
    AFA_ELEVATED_CATEGORIES,
    AFA_ELEVATED_THRESHOLD_PAISE,
    AFA_STANDARD_THRESHOLD_PAISE,
    PRE_DEBIT_NOTICE_LEAD,
    PRE_DEBIT_NOTICE_REQUIRED_FIELDS,
    RBIAdditionalFactorAuth,
    RBIPostDebitNotice,
    RBIPreDebitNotice,
)
from recovery.transport.base import (
    ACTION_MESSAGE,
    ACTION_RETRY,
    CHANNEL_SMS,
    TEMPLATE_POST_DEBIT_NOTICE,
    TEMPLATE_PRE_DEBIT_NOTICE,
    Action,
)
from tests.conftest import make_state
from tests.policy_helpers import context_for, verdict_for

# --------------------------------------------------------------------------------
# Thresholds are integer paise. 15000.0 rupees would usually compare correctly, which
# is worse than always being wrong.
# --------------------------------------------------------------------------------


def test_afa_thresholds_are_integer_paise_not_float_rupees():
    assert AFA_STANDARD_THRESHOLD_PAISE == 1_500_000  # Rs 15,000
    assert AFA_ELEVATED_THRESHOLD_PAISE == 10_000_000  # Rs 1,00,000
    assert type(AFA_STANDARD_THRESHOLD_PAISE) is int
    assert type(AFA_ELEVATED_THRESHOLD_PAISE) is int


# --------------------------------------------------------------------------------
# Additional Factor of Authentication -- amount thresholds
# --------------------------------------------------------------------------------


def _debit(amount_paise: int, *, category="standard", first_debit=False, afa=False, hour=24):
    state = make_state(
        amount_paise=amount_paise,
        mandate_category=category,
        mandate_first_debit=first_debit,
        decline_reason="insufficient_funds",
        method=Method.CARD,
    )
    action = Action(
        type=ACTION_RETRY,
        case_id=state.case_id,
        at_hour=hour,
        idempotency_key="k",
        metadata={"afa_completed": True} if afa else {},
    )
    return action, state


def test_afa_required_above_fifteen_thousand_rupees_on_a_standard_mandate():
    action, state = _debit(1_500_100)  # Rs 15,001
    assert verdict_for(RBIAdditionalFactorAuth(), action, state).allow is False


def test_afa_not_required_below_fifteen_thousand_rupees_on_a_standard_mandate():
    action, state = _debit(1_499_900)  # Rs 14,999
    assert verdict_for(RBIAdditionalFactorAuth(), action, state).allow is True


def test_a_high_value_standard_debit_is_allowed_once_afa_is_completed():
    action, state = _debit(1_500_100, afa=True)
    assert verdict_for(RBIAdditionalFactorAuth(), action, state).allow is True


@pytest.mark.parametrize("category", sorted(AFA_ELEVATED_CATEGORIES))
def test_afa_threshold_is_one_lakh_for_insurance_mutual_fund_and_credit_card_bills(category):
    below, state_below = _debit(9_999_900, category=category)  # Rs 99,999
    assert verdict_for(RBIAdditionalFactorAuth(), below, state_below).allow is True

    above, state_above = _debit(10_000_100, category=category)  # Rs 1,00,001
    assert verdict_for(RBIAdditionalFactorAuth(), above, state_above).allow is False


def test_the_elevated_categories_are_exactly_insurance_mutual_funds_and_credit_card_bills():
    assert AFA_ELEVATED_CATEGORIES == frozenset(
        {"insurance", "mutual_fund", "credit_card_bill"}
    )


def test_a_standard_category_debit_does_not_get_the_elevated_threshold():
    """Rs 50,000 is under the elevated ceiling but over the standard one."""
    action, state = _debit(5_000_000, category="standard")
    assert verdict_for(RBIAdditionalFactorAuth(), action, state).allow is False


# --------------------------------------------------------------------------------
# Additional Factor of Authentication -- the first debit under a mandate
# --------------------------------------------------------------------------------


def test_afa_required_on_first_debit_regardless_of_amount():
    """The rule the research brief missed. It is not an amount rule.

    A one-rupee first debit under a new mandate needs AFA just as a one-lakh one does.
    """
    action, state = _debit(100, first_debit=True)  # Rs 1.00
    verdict = verdict_for(RBIAdditionalFactorAuth(), action, state)
    assert verdict.allow is False
    assert verdict.evidence["first_debit"] is True
    assert verdict.evidence["amount_paise"] == 100


def test_a_one_rupee_first_debit_is_allowed_once_afa_is_completed():
    action, state = _debit(100, first_debit=True, afa=True)
    assert verdict_for(RBIAdditionalFactorAuth(), action, state).allow is True


def test_a_one_rupee_subsequent_debit_needs_no_afa():
    """The contrast that shows the first-debit rule is doing the work, not the amount."""
    action, state = _debit(100, first_debit=False)
    assert verdict_for(RBIAdditionalFactorAuth(), action, state).allow is True


def test_afa_applies_to_money_movement_and_not_to_a_message():
    state = make_state(amount_paise=5_000_000, mandate_first_debit=True)
    action = Action(
        type=ACTION_MESSAGE,
        case_id=state.case_id,
        at_hour=10,
        idempotency_key="k",
        channel=CHANNEL_SMS,
        template="payment_reminder",
    )
    assert verdict_for(RBIAdditionalFactorAuth(), action, state).allow is True


# --------------------------------------------------------------------------------
# Pre-debit notification -- at least 24 hours before the debit
# --------------------------------------------------------------------------------


def _complete_notice() -> dict:
    return {
        "merchant_name": "Example Retail Pvt Ltd",
        "amount_paise": 50_000,
        "debit_datetime": "2026-08-14T10:00:00+05:30",
        "mandate_reference": "MND-000123",
    }


def _emandate_debit(hour: int = 48):
    state = make_state(
        decline_reason="insufficient_funds",
        method=Method.EMANDATE,
        is_recurring=True,
        amount_paise=50_000,
    )
    action = Action(
        type=ACTION_RETRY, case_id=state.case_id, at_hour=hour, idempotency_key="k"
    )
    return action, state


def test_an_emandate_debit_with_no_pre_debit_notice_at_all_is_denied():
    action, state = _emandate_debit()
    assert verdict_for(RBIPreDebitNotice(), action, state).allow is False


def test_a_pre_debit_notice_sent_twenty_three_hours_fifty_nine_minutes_prior_is_denied():
    """The boundary from below. Just under the window is still outside it."""
    action, state = _emandate_debit(hour=48)
    debit_ts = state.failed_at + timedelta(hours=48)
    ctx = context_for(
        state,
        at_hour=48,
        notices=[(debit_ts - timedelta(hours=23, minutes=59), _complete_notice())],
    )
    assert RBIPreDebitNotice().check(action, ctx).allow is False


def test_a_pre_debit_notice_sent_twenty_four_hours_one_minute_prior_is_allowed():
    """The boundary from above."""
    action, state = _emandate_debit(hour=48)
    debit_ts = state.failed_at + timedelta(hours=48)
    ctx = context_for(
        state,
        at_hour=48,
        notices=[(debit_ts - timedelta(hours=24, minutes=1), _complete_notice())],
    )
    assert RBIPreDebitNotice().check(action, ctx).allow is True


def test_a_pre_debit_notice_sent_exactly_twenty_four_hours_prior_is_allowed_deterministically():
    """The clock-skew case. Evaluated at exactly the boundary, twice, with no
    dependence on wall-clock time -- phase 7 attacks this adversarially."""
    action, state = _emandate_debit(hour=48)
    debit_ts = state.failed_at + timedelta(hours=48)
    notice_ts = debit_ts - PRE_DEBIT_NOTICE_LEAD

    at_boundary = dict(at_hour=48, notices=[(notice_ts, _complete_notice())])
    first = RBIPreDebitNotice().check(action, context_for(state, **at_boundary))
    second = RBIPreDebitNotice().check(action, context_for(state, **at_boundary))
    assert first.allow is True
    assert first.allow == second.allow
    assert first.evidence == second.evidence


@pytest.mark.parametrize("missing", sorted(PRE_DEBIT_NOTICE_REQUIRED_FIELDS))
def test_a_pre_debit_notice_missing_a_required_field_is_denied(missing):
    """Merchant name, amount, date and time of the debit, and the mandate reference.
    One test per field, because a notice missing any of them is not a notice."""
    action, state = _emandate_debit(hour=48)
    notice = _complete_notice()
    del notice[missing]
    ctx = context_for(state, at_hour=48, notices=[(state.failed_at + timedelta(hours=1), notice)])
    verdict = RBIPreDebitNotice().check(action, ctx)
    assert verdict.allow is False
    assert missing in verdict.evidence["missing_fields"]


def test_the_required_pre_debit_notice_fields_are_the_four_the_framework_names():
    assert PRE_DEBIT_NOTICE_REQUIRED_FIELDS == frozenset(
        {"merchant_name", "amount_paise", "debit_datetime", "mandate_reference"}
    )


def test_the_pre_debit_notice_lead_time_is_twenty_four_hours():
    assert PRE_DEBIT_NOTICE_LEAD == timedelta(hours=24)


def test_a_complete_and_timely_notice_allows_the_emandate_debit():
    action, state = _emandate_debit(hour=48)
    ctx = context_for(
        state, at_hour=48, notices=[(state.failed_at + timedelta(hours=2), _complete_notice())]
    )
    verdict = RBIPreDebitNotice().check(action, ctx)
    assert verdict.allow is True
    assert verdict.evidence["lead_hours"] >= 24


def test_the_pre_debit_rule_is_scoped_to_emandate_debits():
    """A documented scoping decision, not an omission: recurring card-on-file and UPI
    Autopay debits carry analogous notification duties under their own rails, and this
    project does not encode those."""
    state = make_state(decline_reason="insufficient_funds", method=Method.UPI, is_recurring=True)
    action = Action(type=ACTION_RETRY, case_id=state.case_id, at_hour=48, idempotency_key="k")
    verdict = verdict_for(RBIPreDebitNotice(), action, state)
    assert verdict.allow is True
    assert verdict.evidence.get("applicable") is False


# --------------------------------------------------------------------------------
# Post-debit notification -- grievance redressal
# --------------------------------------------------------------------------------


def test_a_post_debit_notification_without_grievance_redressal_details_is_denied():
    state = make_state(method=Method.EMANDATE)
    action = Action(
        type=ACTION_MESSAGE,
        case_id=state.case_id,
        at_hour=50,
        idempotency_key="k",
        channel=CHANNEL_SMS,
        template=TEMPLATE_POST_DEBIT_NOTICE,
        metadata={"merchant_name": "Example Retail Pvt Ltd"},
    )
    verdict = verdict_for(RBIPostDebitNotice(), action, state)
    assert verdict.allow is False
    assert "grievance" in verdict.reason.lower()


def test_a_post_debit_notification_with_grievance_redressal_details_is_allowed():
    state = make_state(method=Method.EMANDATE)
    action = Action(
        type=ACTION_MESSAGE,
        case_id=state.case_id,
        at_hour=50,
        idempotency_key="k",
        channel=CHANNEL_SMS,
        template=TEMPLATE_POST_DEBIT_NOTICE,
        metadata={"grievance_contact": "grievance@example.com / 1800-000-000"},
    )
    assert verdict_for(RBIPostDebitNotice(), action, state).allow is True


def test_an_empty_grievance_field_does_not_count_as_details():
    state = make_state(method=Method.EMANDATE)
    action = Action(
        type=ACTION_MESSAGE,
        case_id=state.case_id,
        at_hour=50,
        idempotency_key="k",
        channel=CHANNEL_SMS,
        template=TEMPLATE_POST_DEBIT_NOTICE,
        metadata={"grievance_contact": "   "},
    )
    assert verdict_for(RBIPostDebitNotice(), action, state).allow is False


def test_the_post_debit_rule_does_not_constrain_an_ordinary_reminder():
    state = make_state()
    action = Action(
        type=ACTION_MESSAGE,
        case_id=state.case_id,
        at_hour=50,
        idempotency_key="k",
        channel=CHANNEL_SMS,
        template="payment_reminder",
    )
    assert verdict_for(RBIPostDebitNotice(), action, state).allow is True


# --------------------------------------------------------------------------------
# The rules cite their source at the rule, not only in the module docstring
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule", [RBIPreDebitNotice(), RBIAdditionalFactorAuth(), RBIPostDebitNotice()]
)
def test_every_rbi_rule_carries_its_citation(rule):
    citation = getattr(rule, "citation", "")
    assert "RBI" in citation
    assert "E-mandate" in citation
    assert "2026" in citation


# --------------------------------------------------------------------------------
# End to end, through the gate
# --------------------------------------------------------------------------------


def test_an_emandate_retry_without_a_notice_is_blocked_before_it_reaches_the_transport(tmp_path):
    from tests.conftest import make_case, make_gate

    case = make_case("case-emd", decline_reason="insufficient_funds", method=Method.EMANDATE)
    gate = make_gate(tmp_path, [case])
    state = gate.transport.observe("case-emd", 48)
    action = Action(type=ACTION_RETRY, case_id="case-emd", at_hour=48, idempotency_key="k")

    decision, result = gate.execute(action, state)
    assert decision.allowed is False
    assert result is None
    assert gate.transport.outcomes()[0].attempts == 0
    assert any(v.rule_id == "RBIPreDebitNotice" for v in decision.denials())


def test_a_notice_then_a_debit_is_allowed_through_the_gate(tmp_path):
    from tests.conftest import make_case, make_gate

    case = make_case("case-emd2", decline_reason="insufficient_funds", method=Method.EMANDATE)
    gate = make_gate(tmp_path, [case])

    notice = Action(
        type=ACTION_MESSAGE,
        case_id="case-emd2",
        at_hour=10,
        idempotency_key="notice-1",
        channel=CHANNEL_SMS,
        template=TEMPLATE_PRE_DEBIT_NOTICE,
        metadata=_complete_notice(),
    )
    notice_decision, _ = gate.execute(notice, gate.transport.observe("case-emd2", 10))
    assert notice_decision.allowed is True, notice_decision.denials()

    debit = Action(
        type=ACTION_RETRY,
        case_id="case-emd2",
        at_hour=40,
        idempotency_key="retry-1",
        metadata={},
    )
    decision, _ = gate.execute(debit, gate.transport.observe("case-emd2", 40))
    pre_debit = [v for v in decision.verdicts if v.rule_id == "RBIPreDebitNotice"]
    assert pre_debit and pre_debit[0].allow is True
