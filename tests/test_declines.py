"""The decline taxonomy's load-bearing properties."""

from __future__ import annotations

import pytest

from recovery import declines
from recovery.declines import (
    REASONS,
    DeclineClass,
    Method,
    UnknownDeclineReason,
    klass_of,
    lookup,
    reasons_for_method,
)


def test_unknown_code_raises_rather_than_defaulting_to_soft():
    """The single most dangerous line we could write in declines.py is
    ``reasons.get(code, SOFT_DEFAULT)``. Assert explicitly that we did not."""
    with pytest.raises(UnknownDeclineReason):
        lookup("not_a_real_code")

    # And not merely "raises something" -- assert it never yields a SOFT fallback.
    try:
        result = lookup("not_a_real_code")
    except UnknownDeclineReason:
        result = None
    assert result is None

    with pytest.raises(UnknownDeclineReason):
        klass_of("not_a_real_code")


def test_unknown_decline_reason_is_a_keyerror():
    """So that a caller catching KeyError around a taxonomy lookup still catches it."""
    assert issubclass(UnknownDeclineReason, KeyError)


def test_hard_and_action_reasons_can_never_be_retried_into_success():
    for reason in REASONS.values():
        if reason.klass in (DeclineClass.HARD, DeclineClass.ACTION):
            assert reason.retry_success_base == 0.0, reason.code
            assert not reason.is_retryable, reason.code


def test_some_action_reasons_self_heal():
    """ACTION reasons recover without intervention -- that unassisted recovery is the
    mechanism that inflates naive gross-recovery numbers, so it must be modelled."""
    action = [r for r in REASONS.values() if r.klass is DeclineClass.ACTION]
    assert action
    assert any(r.self_heal_rate > 0.0 for r in action)


def test_hard_reasons_never_self_heal():
    for reason in REASONS.values():
        if reason.klass is DeclineClass.HARD:
            assert reason.self_heal_rate == 0.0, reason.code


def test_population_weights_sum_to_one():
    assert abs(sum(r.population_weight for r in REASONS.values()) - 1.0) < 1e-9


def test_all_three_classes_are_populated():
    by_class = {k: 0 for k in DeclineClass}
    for reason in REASONS.values():
        by_class[reason.klass] += 1
    assert all(count > 0 for count in by_class.values()), by_class


def test_expected_codes_are_present():
    expected = {
        "insufficient_funds",
        "issuer_unavailable",
        "gateway_timeout",
        "do_not_honour",
        "risk_declined",
        "stolen_card",
        "lost_card",
        "invalid_account",
        "invalid_vpa",
        "account_closed",
        "expired_card",
        "mandate_revoked",
        "mandate_paused",
        "authentication_failed",
        "card_not_enrolled",
    }
    assert expected <= set(REASONS)


def test_every_method_has_at_least_one_reason():
    """Otherwise cohort generation would be unable to draw a reason for that method."""
    for method in Method:
        assert reasons_for_method(method), method


def test_module_docstring_states_its_provenance():
    doc = declines.__doc__ or ""
    assert "approximation" in doc
    assert "priors" in doc
