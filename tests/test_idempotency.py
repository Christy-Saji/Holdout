"""The double-charge guard.

An idempotency key is the identity of an action, not a description of it. That is what
makes "retry after a timeout" safe: the caller reuses the key and the second call is
refused, so a network failure between request and response cannot become a second
debit on a customer's account.
"""

from __future__ import annotations

from recovery.ledger import KIND_ACTION, KIND_POLICY_CHECK, read_entries
from recovery.policy import rules
from recovery.transport.base import ACTION_MESSAGE, ACTION_RETRY, CHANNEL_EMAIL, Action
from tests.conftest import make_case, make_gate, make_state
from tests.policy_helpers import verdict_for

RECOVERABLE = dict(decline_reason="insufficient_funds", amount_paise=500_000)


def _retry(case_id: str, hour: int, key: str) -> Action:
    return Action(type=ACTION_RETRY, case_id=case_id, at_hour=hour, idempotency_key=key)


# --------------------------------------------------------------------------------
# The rule in isolation
# --------------------------------------------------------------------------------


def test_a_fresh_key_is_allowed():
    state = make_state(**RECOVERABLE)
    action = _retry(state.case_id, 48, "retry-1")
    assert verdict_for(rules.Idempotency(), action, state).allow is True


def test_a_key_already_used_is_denied():
    state = make_state(**RECOVERABLE)
    action = _retry(state.case_id, 48, "retry-1")
    verdict = verdict_for(rules.Idempotency(), action, state, used_keys=["retry-1"])
    assert verdict.allow is False
    assert verdict.evidence["idempotency_key"] == "retry-1"


# --------------------------------------------------------------------------------
# Through the gate
# --------------------------------------------------------------------------------


def test_the_same_key_executes_once_and_the_ledger_shows_it(tmp_path):
    """Two policy checks, one action. The refusal is recorded rather than silent --
    a blocked double-charge is precisely the kind of thing an audit trail is for."""
    case = make_case("case-idem", **RECOVERABLE)
    gate = make_gate(tmp_path, [case])
    state = gate.transport.observe("case-idem", 48)

    first_decision, first_result = gate.execute(_retry("case-idem", 48, "retry-1"), state)
    second_state = gate.transport.observe("case-idem", 48)
    second_decision, second_result = gate.execute(_retry("case-idem", 48, "retry-1"), second_state)
    gate.ledger.close()

    assert first_decision.allowed is True, [v.rule_id for v in first_decision.denials()]
    assert first_result is not None
    assert second_decision.allowed is False
    assert second_result is None

    rows = list(read_entries(gate.ledger.path))
    assert sum(1 for r in rows if r["kind"] == KIND_POLICY_CHECK) == 2
    assert sum(1 for r in rows if r["kind"] == KIND_ACTION) == 1


def test_the_customer_is_charged_exactly_once(tmp_path):
    """Assert on the transport, not only on the return value. A gate that logs a
    denial and then acts anyway would pass a weaker test."""
    case = make_case("case-idem2", **RECOVERABLE)
    gate = make_gate(tmp_path, [case])

    for _ in range(5):
        state = gate.transport.observe("case-idem2", 48)
        gate.execute(_retry("case-idem2", 48, "retry-1"), state)
    gate.ledger.close()

    outcome = gate.transport.outcomes()[0]
    assert outcome.attempts == 1
    assert outcome.cost_paise == 300


def test_the_denial_names_the_idempotency_rule(tmp_path):
    case = make_case("case-idem3", **RECOVERABLE)
    gate = make_gate(tmp_path, [case])

    state = gate.transport.observe("case-idem3", 48)
    gate.execute(_retry("case-idem3", 48, "retry-1"), state)
    second, _ = gate.execute(_retry("case-idem3", 48, "retry-1"), gate.transport.observe("case-idem3", 48))
    gate.ledger.close()

    assert "Idempotency" in {v.rule_id for v in second.denials()}


def test_a_different_key_for_the_same_logical_action_still_executes(tmp_path):
    """The key is the identity. Two genuinely distinct attempts carry distinct keys,
    and the guard does not stand in their way."""
    case = make_case("case-idem4", **RECOVERABLE)
    gate = make_gate(tmp_path, [case])

    first, _ = gate.execute(_retry("case-idem4", 48, "retry-1"), gate.transport.observe("case-idem4", 48))
    second, _ = gate.execute(_retry("case-idem4", 96, "retry-2"), gate.transport.observe("case-idem4", 96))
    gate.ledger.close()

    assert first.allowed is True
    assert "Idempotency" not in {v.rule_id for v in second.denials()}


def test_concurrent_replay_is_denied_without_an_intervening_read(tmp_path):
    """The same key evaluated twice back to back, with nothing re-read from disk in
    between. The gate's own index reflects the first execution immediately, so a
    duplicate cannot slip through the window between write and reload."""
    case = make_case("case-idem5", **RECOVERABLE)
    gate = make_gate(tmp_path, [case])
    state = gate.transport.observe("case-idem5", 48)

    action = _retry("case-idem5", 48, "retry-1")
    first, _ = gate.execute(action, state)
    second, _ = gate.execute(action, state)  # same stale state object, no re-observe
    gate.ledger.close()

    assert first.allowed is True
    assert second.allowed is False
    assert "Idempotency" in {v.rule_id for v in second.denials()}
    assert gate.transport.outcomes()[0].attempts == 1


def test_a_denied_action_does_not_consume_its_key(tmp_path):
    """A key is spent by execution, not by evaluation. An action denied for an
    unrelated reason must remain retryable once that reason clears."""
    case = make_case("case-idem6", decline_reason="insufficient_funds", amount_paise=500_000)
    gate = make_gate(tmp_path, [case])

    too_soon = gate.transport.observe("case-idem6", 2)
    denied, _ = gate.execute(_retry("case-idem6", 2, "retry-1"), too_soon)
    assert denied.allowed is False
    assert "CoolingOff" in {v.rule_id for v in denied.denials()}

    later = gate.transport.observe("case-idem6", 48)
    allowed, result = gate.execute(_retry("case-idem6", 48, "retry-1"), later)
    gate.ledger.close()

    assert allowed.allowed is True, [v.rule_id for v in allowed.denials()]
    assert result is not None


def test_keys_are_scoped_across_action_types(tmp_path):
    """A message and a retry sharing one key is a caller bug; the guard catches it
    rather than quietly executing both."""
    case = make_case("case-idem7", **RECOVERABLE)
    gate = make_gate(tmp_path, [case])

    message = Action(
        type=ACTION_MESSAGE,
        case_id="case-idem7",
        at_hour=10,
        idempotency_key="shared",
        channel=CHANNEL_EMAIL,
        template="payment_reminder",
    )
    first, _ = gate.execute(message, gate.transport.observe("case-idem7", 10))
    second, _ = gate.execute(_retry("case-idem7", 48, "shared"), gate.transport.observe("case-idem7", 48))
    gate.ledger.close()

    assert first.allowed is True, [v.rule_id for v in first.denials()]
    assert "Idempotency" in {v.rule_id for v in second.denials()}


def test_the_index_rebuilt_from_a_ledger_file_still_refuses_a_used_key(tmp_path):
    """Idempotency state lives in history. Reconstructing that history from the
    committed ledger must reach the same conclusion as the live run did."""
    from recovery.policy.engine import LedgerIndex

    case = make_case("case-idem8", **RECOVERABLE)
    gate = make_gate(tmp_path, [case])
    gate.execute(_retry("case-idem8", 48, "retry-1"), gate.transport.observe("case-idem8", 48))
    gate.ledger.close()

    rebuilt = LedgerIndex.from_ledger_file(gate.ledger.path)
    assert "retry-1" in rebuilt.used_keys
    assert rebuilt.case_spend.get("case-idem8") == 300
