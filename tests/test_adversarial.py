"""Five deliberate attacks on the system, all runnable offline with no credentials.

These are not hardening tests. Each one is an attempt to make the system do the
specific wrong thing a payments engineer would look for first, and each either gets
handled or gets written up as a known gap in ``WHAT_BROKE.md`` with a number attached.

The numbers these produce are quoted in ``WHAT_BROKE.md``. If a bound here changes, that
document is wrong until it is updated -- which is the point of asserting on the amount
rather than merely on the shape.

Nothing here touches the network. The live-transport tests live at the bottom behind a
``live`` marker and skip without ``RAZORPAY_KEY_ID``, so ``pytest`` stays green for a
reviewer who has no keys.
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from recovery.agent import tools
from recovery.agent.tools import MAX_PLANNED_ACTIONS, Session, bind, unbind
from recovery.ledger import KIND_ACTION, KIND_OBSERVATION, KIND_POLICY_CHECK, Ledger, read_entries
from recovery.policy.rbi import RBIPreDebitNotice
from recovery.transport import razorpay_test, webhooks
from recovery.declines import Method
from recovery.transport.base import (
    ACTION_MESSAGE,
    ACTION_RETRY,
    CHANNEL_SMS,
    STATUS_RECOVERED,
    Action,
    SimParams,
)
from recovery.transport.mock import MockTransport
from recovery.transport.webhooks import (
    EVENT_PAYMENT_CAPTURED,
    EVENT_PAYMENT_FAILED,
    Receipt,
    WebhookReceiver,
    parse,
    verify,
)
from tests.conftest import make_case, make_latents, make_gate, make_state
from tests.policy_helpers import verdict_for

ROOT = Path(__file__).resolve().parent.parent

WEBHOOK_SECRET = "whsec_adversarial_fixture"
"""A fixture secret. Signature verification is pure HMAC, so it needs no account."""


# ==================================================================================
# 1. Duplicate webhook delivery
# ==================================================================================


def webhook_body(case_id: str, amount_paise: int, *, event: str = EVENT_PAYMENT_CAPTURED,
                 payment_id: str = "pay_ADVERSARIAL1") -> str:
    """A Razorpay webhook envelope, shaped as the real one is."""
    return json.dumps(
        {
            "entity": "event",
            "event": event,
            "payload": {
                "payment": {
                    "entity": {
                        "id": payment_id,
                        "amount": amount_paise,  # paise, as Razorpay sends it
                        "currency": "INR",
                        "status": "captured",
                        "notes": {"case_id": case_id},
                    }
                }
            },
        },
        sort_keys=True,
    )


def sign(body: str, secret: str = WEBHOOK_SECRET) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def headers_for(body: str, event_id: str) -> dict[str, str]:
    # Deliberately capitalised differently from the constants: real frameworks
    # normalise header case inconsistently and the lookup must not care.
    return {
        "X-Razorpay-Event-Id": event_id,
        "X-Razorpay-Signature": sign(body),
        "Content-Type": "application/json",
    }


def test_duplicate_webhook_recovers_the_case_exactly_once(tmp_path):
    """The same success event, delivered twice, must credit the money once.

    At-least-once delivery is the contract every webhook producer actually offers. The
    duplicate arrives precisely when the first delivery's response was lost -- which is
    the case where the first delivery *did* take effect.
    """
    case = make_case("case-00001", amount_paise=250_000, p_self_heal=0.0)
    transport = MockTransport([case], seed=42)
    ledger = Ledger(tmp_path / "webhook.jsonl", run_id="run-adversarial")

    follow_ups: list[Receipt] = []
    receiver = WebhookReceiver(
        transport=transport,
        ledger=ledger,
        arm="agent",
        on_recovered=follow_ups.append,
    )

    body = webhook_body(case.case_id, case.amount_paise)
    headers = headers_for(body, "evt_DUPLICATE01")

    verify(body, headers, WEBHOOK_SECRET)
    first = receiver.deliver(parse(body, headers, at_hour=100))

    # Byte-identical redelivery, exactly as Razorpay retries it.
    verify(body, headers, WEBHOOK_SECRET)
    second = receiver.deliver(parse(body, headers, at_hour=101))
    ledger.close()

    assert first.applied is True and first.duplicate is False
    assert first.amount_paise == 250_000
    assert second.applied is False and second.duplicate is True
    assert second.amount_paise == 0, "a duplicate must never carry money"

    # One recovery, at the first delivery's hour, not the redelivery's.
    outcome = transport.outcomes()[0]
    assert outcome.recovered is True
    assert outcome.recovered_at_hour == 100
    assert receiver.applied_paise() == 250_000

    # No follow-up action on the second delivery. This is the guarantee that stops a
    # duplicate from triggering a second "payment received" message to the customer.
    assert len(follow_ups) == 1

    rows = [r for r in read_entries(tmp_path / "webhook.jsonl") if r["kind"] == KIND_OBSERVATION]
    assert len(rows) == 2, "both deliveries are recorded; the duplicate is not hidden"
    credited = sum(r["result"]["webhook"]["amount_paise"] for r in rows)
    assert credited == 250_000, "the ledger must not double-count the amount"
    assert sum(r["cost_paise"] for r in rows) == 0


def test_duplicate_webhook_after_a_retry_already_recovered_the_case(tmp_path):
    """A webhook confirming a payment the simulator already recorded adds nothing.

    The dedupe key is the event id, but the *idempotence* is in ``mark_recovered``. This
    covers the case where the two paths disagree: a genuinely new event id describing a
    payment already known about.
    """
    case = make_case("case-00002", amount_paise=90_000, p_self_heal=0.0, retry_success_base=1.0)
    transport = MockTransport([case], seed=42)
    ledger = Ledger(tmp_path / "webhook.jsonl", run_id="run-adversarial")
    receiver = WebhookReceiver(transport=transport, ledger=ledger, arm="agent")

    transport.tick(48)
    assert transport.attempt_retry(case.case_id, 48, "k1").recovered is True

    body = webhook_body(case.case_id, case.amount_paise)
    receipt = receiver.deliver(parse(body, headers_for(body, "evt_LATE01"), at_hour=49))
    ledger.close()

    assert receipt.duplicate is False, "a new event id is not a redelivery"
    assert receipt.applied is False, "but the case had already recovered"
    assert receipt.amount_paise == 0
    assert transport.outcomes()[0].recovered_at_hour == 48


def test_unsigned_and_mis_signed_webhooks_are_refused(tmp_path):
    body = webhook_body("case-00001", 1000)
    with pytest.raises(webhooks.SignatureRejected):
        verify(body, {"X-Razorpay-Signature": "deadbeef"}, WEBHOOK_SECRET)
    with pytest.raises(webhooks.SignatureRejected):
        verify(body, {"X-Razorpay-Event-Id": "evt_1"}, WEBHOOK_SECRET)


def test_a_delivery_without_an_event_id_is_refused():
    """Without the event id a redelivery is indistinguishable from a second payment."""
    body = webhook_body("case-00001", 1000)
    with pytest.raises(webhooks.UnroutableEvent):
        parse(body, {"X-Razorpay-Signature": sign(body)}, at_hour=0)


def test_a_non_recovery_event_never_marks_a_case_recovered(tmp_path):
    """``RECOVERY_EVENTS`` is an allowlist, for the same reason unknown declines raise."""
    case = make_case("case-00003", p_self_heal=0.0)
    transport = MockTransport([case], seed=42)
    ledger = Ledger(tmp_path / "webhook.jsonl", run_id="run-adversarial")
    receiver = WebhookReceiver(transport=transport, ledger=ledger, arm="agent")

    body = webhook_body(case.case_id, case.amount_paise, event=EVENT_PAYMENT_FAILED)
    receipt = receiver.deliver(parse(body, headers_for(body, "evt_FAILED01"), at_hour=10))
    ledger.close()

    assert receipt.applied is False
    assert transport.outcomes()[0].recovered is False


# ==================================================================================
# 2. The customer pays mid-sequence
# ==================================================================================


def find_case_that_self_heals_between(lo: int, hi: int, *, seed: int = 42):
    """A real case whose latent self-heal lands in ``[lo, hi]``.

    Deliberately not ``mark_recovered``: this scenario is about a customer paying of
    their own accord while the dunning sequence is still queued, and the honest version
    of that is the simulator's own self-heal draw rather than an injected event. The
    search is over case ids, which key the CRN stream, so it is deterministic.
    """
    for i in range(500):
        case = make_case(f"case-{i:05d}", p_self_heal=0.5)
        transport = MockTransport([case], seed=seed)
        for hour in range(hi + 1):
            transport.tick(hour)
            if transport.observe(case.case_id, hour).status == STATUS_RECOVERED:
                if lo <= hour <= hi:
                    return case, hour
                break
    raise AssertionError(f"no case in the first 500 self-heals between hours {lo} and {hi}")


def test_queued_actions_are_blocked_once_the_customer_has_already_paid(tmp_path):
    """The bug that dunts someone who already paid.

    A case heals on its own around hour 100 with a retry queued for +10 and an SMS for
    +20. Both must be refused by ``TerminalState``, and both refusals must be in the
    ledger -- a blocked action that leaves no trace is indistinguishable from an action
    nobody attempted.
    """
    case, heal_hour = find_case_that_self_heals_between(80, 130)
    gate = make_gate(tmp_path, [case], arm="agent")
    transport = gate.transport

    for hour in range(heal_hour + 1):
        transport.tick(hour)
    assert transport.observe(case.case_id, heal_hour).status == STATUS_RECOVERED

    retry_hour, sms_hour = heal_hour + 10, heal_hour + 20
    queued = [
        Action(
            type=ACTION_RETRY,
            case_id=case.case_id,
            at_hour=retry_hour,
            idempotency_key="retry-after-payment",
        ),
        Action(
            type=ACTION_MESSAGE,
            case_id=case.case_id,
            at_hour=sms_hour,
            idempotency_key="nudge-after-payment",
            channel=CHANNEL_SMS,
            template="payment_reminder",
        ),
    ]

    for action in queued:
        transport.tick(action.at_hour)
        state = transport.observe(case.case_id, action.at_hour)
        decision, result = gate.execute(action, state)
        assert decision.allowed is False
        assert result is None, "nothing may reach the transport"
        assert "TerminalState" in {v.rule_id for v in decision.denials()}
    gate.ledger.close()

    rows = list(read_entries(gate.ledger.path))
    denials = [r for r in rows if r["kind"] == KIND_POLICY_CHECK and r["allowed"] is False]
    assert len(denials) == 2, "both refusals are in the ledger"
    assert all(
        any(v["rule_id"] == "TerminalState" and not v["allow"] for v in r["verdicts"])
        for r in denials
    )
    assert [r for r in rows if r["kind"] == KIND_ACTION] == [], "no action executed"
    assert sum(r["cost_paise"] for r in rows) == 0, "a refused action costs nothing"

    # The engine does not short-circuit: every rule still returned a verdict.
    assert all(len(r["verdicts"]) == 12 for r in denials)


# ==================================================================================
# 3. A hard decline mislabelled as soft
# ==================================================================================


MISLABELLED_MAX_ATTEMPTS = 7
"""What the cap actually holds this case to -- **measured, not chosen**. Seven attempts
is more than it looks: each one is a decline the issuer sees. Quoted in
``WHAT_BROKE.md``; if this changes, that document is stale until it is updated."""

MISLABELLED_MAX_WASTE_PAISE = 2_100
"""Rs 21.00 in gateway fees on a payment that could never succeed.

The invoiced number, and the smaller half. The modelled approval-goodwill damage of
seven failed attempts is ``7 * APPROVAL_DEGRADATION_COST_PAISE`` = Rs 49.00, which
appears on no invoice and is the cost that actually matters."""


def test_a_mislabelled_hard_decline_is_stopped_by_the_cap_not_by_the_window(tmp_path):
    """The input is wrong and the system cannot know it. Measure what that costs.

    The case says ``insufficient_funds``. The truth is a dead instrument: no retry can
    ever succeed. The scorer reads the label, so it will authorise retries -- correctly,
    given its inputs. The question is not whether the system avoids this. It cannot. The
    question is whether it stops on its own, and how much it burns first.
    """
    case = make_case(
        "case-mislabelled",
        decline_reason="insufficient_funds",  # the label
        amount_paise=400_000,
        latents=make_latents(  # the truth
            p_self_heal=0.0,
            retry_success_base=0.0,
            intent_to_pay=0.0,
            responsiveness=0.0,
            affinity=0.0,
        ),
    )
    gate = make_gate(tmp_path, [case], arm="agent")
    transport = gate.transport
    params = SimParams()

    executed: list[int] = []
    denials_after_last: list[set[str]] = []

    # A maximally greedy arm: retry every single hour it is allowed to. Nothing but the
    # policy engine stands between this and 336 attempts.
    for hour in range(params.window_h):
        transport.tick(hour)
        state = transport.observe(case.case_id, hour)
        if state.is_terminal:
            break
        action = Action(
            type=ACTION_RETRY,
            case_id=case.case_id,
            at_hour=hour,
            idempotency_key=f"greedy-{hour}",
        )
        decision, result = gate.execute(action, state)
        if result is not None:
            executed.append(hour)
            denials_after_last.clear()
        elif executed:
            denials_after_last.append({v.rule_id for v in decision.denials()})
    gate.ledger.close()

    wasted = sum(r["cost_paise"] for r in read_entries(gate.ledger.path))

    assert executed, "the cap must not refuse the first attempt; the label looks recoverable"
    assert len(executed) <= MISLABELLED_MAX_ATTEMPTS, (
        f"{len(executed)} attempts on a dead instrument; the cap is not binding"
    )
    assert wasted <= MISLABELLED_MAX_WASTE_PAISE, f"wasted {wasted}p"
    assert transport.outcomes()[0].recovered is False

    # It stopped because the economics stopped it, not because the window ran out.
    assert denials_after_last, "the greedy arm was never refused"
    assert all("AttemptCap" in d for d in denials_after_last), (
        "every refusal after the last attempt must name AttemptCap"
    )
    assert executed[-1] < params.window_h - 24, "it stopped well inside the window"


def test_the_same_case_correctly_labelled_is_refused_immediately(tmp_path):
    """The contrast that makes the previous number meaningful.

    Labelled ``stolen_card``, the identical case costs nothing at all: ``HardDeclineNoRetry``
    refuses before any money moves. The whole of the waste above is attributable to the
    bad label, not to the policy.
    """
    case = make_case("case-labelled", decline_reason="stolen_card", amount_paise=400_000)
    gate = make_gate(tmp_path, [case], arm="agent")
    state = gate.transport.observe(case.case_id, 48)
    action = Action(
        type=ACTION_RETRY, case_id=case.case_id, at_hour=48, idempotency_key="honest-1"
    )
    decision, result = gate.execute(action, state)
    gate.ledger.close()

    assert decision.allowed is False and result is None
    assert "HardDeclineNoRetry" in {v.rule_id for v in decision.denials()}
    assert sum(r["cost_paise"] for r in read_entries(gate.ledger.path)) == 0


# ==================================================================================
# 4. An agent that tries to exceed its budget
# ==================================================================================


def test_budget_ceiling_denies_and_a_looping_agent_spends_nothing(tmp_path):
    """A case too small to be worth working, against an agent that will not stop asking.

    ``BudgetCeiling`` compares cumulative spend against expected recovery, so on a Rs 20
    case the budget is exhausted after a couple of nudges. The scenario then does what a
    badly-behaved model does: re-issues the same denied action over and over.

    What is asserted is the property that holds *regardless of what the model does* --
    a denied action is never queued, never executed and never costs anything, however
    many times it is re-issued. Whether Claude itself reacts to the denial by closing
    the case is not tested here and cannot be: recording that needs an API key and the
    cassettes do not exist. That gap is recorded in ``WHAT_BROKE.md`` rather than papered
    over with a stub that would only be asserting on its own script.
    """
    # Rs 1.00. The ceiling is expected recovery -- about a third of the amount -- so it
    # is 33 paise, and the second 20-paise SMS breaches it. Chosen so that BudgetCeiling
    # and not ContactFrequency is the rule under test: at 24h spacing the frequency cap
    # would not bind until the fourth nudge.
    case = make_case("case-tiny", amount_paise=100, p_self_heal=0.0)
    gate = make_gate(tmp_path, [case], arm="agent")
    state = gate.transport.observe(case.case_id, 0)
    session = Session(gate=gate, state=state, params=SimParams())

    token = bind(session)
    try:
        outcomes = [
            json.loads(
                tools.send_message.call(
                    {
                        "case_id": case.case_id,
                        "channel": CHANNEL_SMS,
                        "template": "payment_reminder",
                        "at_hour": 24 + i * 24,
                        "idempotency_key": f"nudge-{i}",
                    }
                )
            )
            for i in range(MAX_PLANNED_ACTIONS + 4)
        ]

        allowed = [o for o in outcomes if o.get("allowed")]
        refused = [o for o in outcomes if not o.get("allowed")]
        assert refused, "the ceiling never bound; this case is not small enough to test it"
        assert "BudgetCeiling" in {o.get("rule_id") for o in refused} | {
            r for o in refused for r in o.get("all_denials", [])
        }
        assert len(session.planned) == len(allowed) <= MAX_PLANNED_ACTIONS
        assert session.denials == len(refused)

        # The stubborn part: re-issue the *same* denied action ten more times.
        last = refused[-1]
        repeats = [
            json.loads(
                tools.send_message.call(
                    {
                        "case_id": case.case_id,
                        "channel": CHANNEL_SMS,
                        "template": "payment_reminder",
                        "at_hour": last["at_hour"],
                        "idempotency_key": "stubborn",
                    }
                )
            )
            for _ in range(10)
        ]
        assert all(r["allowed"] is False for r in repeats)
        planned_before_close = len(session.planned)

        closed = json.loads(
            tools.close_case.call(
                {"case_id": case.case_id, "reason": "not economic to work"}
            )
        )
    finally:
        unbind(token)
    gate.ledger.close()

    assert planned_before_close == len(allowed), "a denied action is never queued"
    assert closed.get("ok") is not True or session.closed, "closing after denial must work"

    rows = list(read_entries(gate.ledger.path))
    executed = [r for r in rows if r["kind"] == KIND_ACTION]
    assert executed == [], "screening queues actions; it never executes them"
    assert sum(r["cost_paise"] for r in rows) == 0, "a loop against the gate costs no money"

    refusals = [r for r in rows if r["kind"] == KIND_POLICY_CHECK and r["allowed"] is False]
    assert len(refusals) >= 10, "every refusal reaches the ledger, including the repeats"


def test_the_budget_ceiling_is_proportional_to_the_amount(tmp_path):
    """The same policy on a large case allows what it refuses on a small one.

    Without this, the previous test is also consistent with a ceiling that simply
    refuses everything.
    """
    small = make_case("case-small", amount_paise=100, p_self_heal=0.0)
    large = make_case("case-large", amount_paise=2_000_000, p_self_heal=0.0)

    def nudges_allowed(case) -> int:
        gate = make_gate(tmp_path / case.case_id, [case], arm="agent")
        state = gate.transport.observe(case.case_id, 0)
        session = Session(gate=gate, state=state, params=SimParams())
        token = bind(session)
        try:
            for i in range(MAX_PLANNED_ACTIONS):
                tools.send_message.call(
                    {
                        "case_id": case.case_id,
                        "channel": CHANNEL_SMS,
                        "template": "payment_reminder",
                        "at_hour": 24 + i * 24,
                        "idempotency_key": f"nudge-{i}",
                    }
                )
        finally:
            unbind(token)
        gate.ledger.close()
        return len(session.planned)

    assert nudges_allowed(small) < nudges_allowed(large)


# ==================================================================================
# 5. Clock skew across the 24-hour RBI notice boundary
# ==================================================================================


DEBIT_HOUR = 48
"""The debit sits two days after the failure, so a notice's lead time is a real
positive number. Getting this wrong is incident #1 in ``WHAT_BROKE.md``."""


def notice_verdict(minutes_before: int):
    """The pre-debit rule's verdict for a notice sent ``minutes_before`` the debit."""
    state = make_state(
        case=make_case("case-skew", decline_reason="insufficient_funds", method=Method.EMANDATE),
        hour=DEBIT_HOUR,
    )
    debit_at = state.failed_at + timedelta(hours=DEBIT_HOUR)
    action = Action(
        type=ACTION_RETRY,
        case_id=state.case_id,
        at_hour=DEBIT_HOUR,
        idempotency_key="debit-1",
        metadata={"afa_completed": False},
    )
    return verdict_for(
        RBIPreDebitNotice(),
        action,
        state,
        notices=[
            (
                debit_at - timedelta(minutes=minutes_before),
                {
                    "merchant_name": "Merchant",
                    "amount_paise": state.amount_paise,
                    "debit_datetime": debit_at.isoformat(),
                    "mandate_reference": "mandate-case-skew",
                },
            )
        ],
    )


def test_the_boundary_verdicts_are_what_the_framework_requires():
    assert notice_verdict(24 * 60 - 1).allow is False, "23h59m is not 24 hours' notice"
    assert notice_verdict(24 * 60).allow is True, "exactly 24 hours satisfies 'at least 24'"
    assert notice_verdict(24 * 60 + 1).allow is True


def test_the_verdict_is_stable_under_repeated_and_interleaved_evaluation():
    """Phase 4 tested this boundary statically. This is the version that bites.

    A compliance rule whose answer drifts between two evaluations of the same facts --
    because it read a wall clock, or cached across calls, or mutated the history it was
    handed -- fails an audit no matter how correct any single evaluation was.
    """
    skew = [24 * 60 + 1, 24 * 60 - 1, 24 * 60, 24 * 60 - 1, 24 * 60 + 1]
    baseline = {m: notice_verdict(m).allow for m in set(skew)}

    for _ in range(50):
        for minutes in skew:
            v = notice_verdict(minutes)
            assert v.allow is baseline[minutes]
            assert v.evidence["required_lead_hours"] == 24.0


def test_the_verdict_survives_a_process_restart():
    """Same facts, fresh interpreter, byte-identical evidence.

    An in-process repeat cannot catch a value seeded once per process. This can.
    """
    script = (
        "import json,sys;"
        f"sys.path.insert(0,{str(ROOT)!r});"
        "from tests.test_adversarial import notice_verdict;"
        "print(json.dumps({str(m): notice_verdict(m).to_dict() "
        "for m in (24*60-1, 24*60, 24*60+1)}, sort_keys=True))"
    )
    out = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=ROOT, check=True
    )
    restarted = json.loads(out.stdout)
    here = {
        str(m): notice_verdict(m).to_dict() for m in (24 * 60 - 1, 24 * 60, 24 * 60 + 1)
    }
    assert restarted == here


def test_no_module_in_recovery_reads_a_wall_clock():
    """The structural version of the same claim, and the one that keeps holding.

    Every timestamp in this project is the simulated clock. A single ``datetime.now()``
    anywhere in ``recovery/`` would make some verdict depend on when it was evaluated,
    and the two tests above would only catch it if it happened to sit on this one rule.
    """
    forbidden = {"now", "utcnow", "today", "time", "monotonic", "time_ns"}
    offenders = []
    for path in sorted((ROOT / "recovery").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # The AST, not a grep: five docstrings in ``recovery/`` say "never
            # datetime.now()", and a substring search reports all five as violations.
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in forbidden
            ):
                offenders.append(
                    f"{path.relative_to(ROOT).as_posix()}:{node.lineno} .{node.func.attr}()"
                )
    assert offenders == []


# ==================================================================================
# Live test-mode transport -- skipped without credentials
# ==================================================================================


live = pytest.mark.skipif(
    not razorpay_test.has_credentials(),
    reason="no Razorpay test-mode keys in .env; the live transport is optional",
)


def test_the_live_transport_refuses_a_live_key():
    """The one live-transport guarantee worth having with no keys at all.

    Pointing a dunning system at a live account by an environment-variable mistake is
    the worst accident available here, so the refusal is asserted unconditionally.
    """
    from recovery.transport.razorpay_test import LiveKeyRefused, credentials_from_env

    with pytest.raises(LiveKeyRefused):
        credentials_from_env(
            {"RAZORPAY_KEY_ID": "rzp_live_ABCDEFGH", "RAZORPAY_KEY_SECRET": "secret"}
        )


def test_missing_credentials_name_the_variable():
    from recovery.transport.razorpay_test import MissingCredentials, credentials_from_env

    with pytest.raises(MissingCredentials) as exc:
        credentials_from_env({"RAZORPAY_KEY_ID": "rzp_test_ABC", "RAZORPAY_KEY_SECRET": ""})
    assert "RAZORPAY_KEY_SECRET" in str(exc.value)


@live
@pytest.mark.live
def test_live_order_is_created_in_paise(tmp_path):
    """One real test-mode order, asserting the amount round-trips as integer paise."""
    from recovery.transport.razorpay_test import RazorpayTestTransport

    case = make_case("case-live-01", amount_paise=123_45)
    transport = RazorpayTestTransport.from_env([case])
    result = transport.attempt_retry(case.case_id, at_hour=48, idempotency_key="live-1")

    assert result.ok is True, result.detail
    assert result.recovered is False, "creating an order does not collect money"
    order = transport.client.order.fetch(result.detail["order_id"])
    assert order["amount"] == 123_45
    assert order["currency"] == "INR"


@live
@pytest.mark.live
def test_live_payment_link_is_created(tmp_path):
    from recovery.transport.razorpay_test import RazorpayTestTransport

    case = make_case("case-live-02", amount_paise=50_000)
    transport = RazorpayTestTransport.from_env([case])
    result = transport.send_message(
        case.case_id,
        channel=CHANNEL_SMS,
        template="payment_reminder",
        at_hour=24,
        idempotency_key="live-link-1",
    )
    assert result.ok is True, result.detail
    assert result.detail["short_url"].startswith("https://")
