"""The six tools the model is given, and the per-case session they act on.

**Gating happens inside the tool function, not in a wrapper the model cannot see.**
That is the whole design. A denied action is a *normal return value* --
``{"allowed": false, "rule_id": ..., "reason": ...}`` -- which the model reads and
reacts to by choosing differently. Raising would abort the turn and teach it nothing,
and gating outside the tool would mean the model never learns the constraint exists.
The denial is written to the ledger on its way past, so the blocked-action log builds
itself out of the agent's ordinary operation rather than being assembled afterwards.

**Three of the six touch nothing.** ``get_case_detail``, ``get_customer_history`` and
``check_policy`` are reads. ``check_policy`` is the interesting one: it is a free,
read-only dry run against exactly the rules that gate the real action, so the model's
cheapest path is to *plan within the constraints* rather than to discover them by being
refused. That matters for the evidence -- a blocked-action log full of the model
bumping into rules it could have queried is much weaker than one where every denial is
a genuine surprise.

**Why a ContextVar rather than closures or bound methods.** ``@beta_tool`` builds the
tool schema from the function signature and docstring, and tools render *first* in the
prompt-cache prefix. Rebuilding the tool objects per case would risk a byte-different
schema and invalidate the cache for every case; a closure over the session would put
the session in the signature. Module-level functions plus a context variable keep the
six schemas byte-identical for the whole run and keep the model's view of each tool to
the arguments it actually chooses.

Tool return values are JSON strings. Tool *inputs* arrive as a dict that the SDK has
already parsed from the model's JSON, so there is no raw string matching anywhere in
this module; each tool validates its arguments and returns an error object rather than
raising.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import timedelta

from anthropic import beta_tool

from recovery.policy.engine import Decision, Gate, LedgerIndex
from recovery.transport.base import (
    ACTION_CLOSE,
    ACTION_MESSAGE,
    ACTION_RETRY,
    CHANNELS,
    TEMPLATE_PAYMENT_REMINDER,
    TEMPLATE_POST_DEBIT_NOTICE,
    TEMPLATE_PRE_DEBIT_NOTICE,
    TEMPLATE_UPDATE_INSTRUMENT,
    Action,
    CaseState,
    SimParams,
)

MERCHANT_NAME = "Razorpay Recovery Demo Merchant"
"""Named on the pre-debit notice. A real deployment reads this from the merchant
record; the synthetic cohort has no merchant table, so it is a constant -- and being a
constant is also what keeps it out of the way of the prompt cache."""

GRIEVANCE_CONTACT = "grievance@merchant.example / 1800-000-0000"
"""Carried by every post-debit notification, because RBIPostDebitNotice requires that a
customer who has just been debited is told in the same message how to complain."""

TEMPLATES = (
    TEMPLATE_PRE_DEBIT_NOTICE,
    TEMPLATE_POST_DEBIT_NOTICE,
    TEMPLATE_UPDATE_INSTRUMENT,
    TEMPLATE_PAYMENT_REMINDER,
)

MAX_PLANNED_ACTIONS = 8
"""A hard stop on plan size, independent of the policy engine.

The engine's ``BudgetCeiling`` and ``AttemptCap`` already make a large plan
uneconomic, but they are per-action judgements and a model that ignored them would
still burn a turn per attempt. This is the structural bound.
"""


# -- the per-case session -----------------------------------------------------------


@dataclass
class Session:
    """What the tools act on for one case. One session per planning call.

    ``provisional`` is the point of this type. The planner screens a whole plan at
    hour 0, so step *k+1* has to be judged against a history in which step *k* has
    already happened -- otherwise a debit at hour 48 is refused for want of the
    pre-debit notice the same plan schedules at hour 24. Approved steps are recorded
    into this copy; the gate's real index is untouched until an action executes.
    """

    gate: Gate
    state: CaseState
    params: SimParams
    provisional: LedgerIndex = field(init=False)
    planned: list[Action] = field(default_factory=list)
    closed: bool = False
    close_reason: str | None = None
    denials: int = 0

    def __post_init__(self) -> None:
        self.provisional = self.gate.index.copy()

    def namespaced(self, key: str) -> str:
        """Scope the model's idempotency key to its case.

        ``LedgerIndex.used_keys`` is global across a run, so two cases that both name
        a key ``retry-1`` would collide and the second would be denied by a rule that
        is supposed to catch a *repeat of the same action*. The model is told about
        this prefix in the tool docstrings so the keys it reads back are not a
        surprise.
        """
        return f"{self.state.case_id}:{key.strip()}"

    def record(self, action: Action) -> None:
        """Fold an approved step into the provisional history and queue it."""
        self.provisional.record_execution(
            action=action,
            state=self.state,
            clock=self.gate.clock_for(self.state, action.at_hour),
            cost_paise=action.cost_paise(self.params.costs),
        )
        self.planned.append(action)

    @property
    def actions(self) -> tuple[Action, ...]:
        """The plan, in firing order. Ties break on type so a run is reproducible."""
        return tuple(sorted(self.planned, key=lambda a: (a.at_hour, a.type, a.idempotency_key)))


_SESSION: ContextVar[Session | None] = ContextVar("recovery_agent_session", default=None)


def bind(session: Session):
    """Install ``session`` as the current one. Returns the token to reset with."""
    return _SESSION.set(session)


def unbind(token) -> None:
    _SESSION.reset(token)


def current() -> Session:
    session = _SESSION.get()
    if session is None:
        raise RuntimeError(
            "no agent session is bound; recovery.agent.tools must be called from "
            "inside a planning run"
        )
    return session


# -- shared helpers ------------------------------------------------------------------


def _error(message: str, **extra) -> str:
    """A validation failure, returned to the model rather than raised at the runner.

    The runner would catch an exception and hand the model a generic error; saying
    exactly what was wrong lets it correct itself on the next turn.
    """
    return json.dumps({"ok": False, "error": message, **extra}, sort_keys=True)


def _wrong_case(case_id: str, session: Session) -> str | None:
    if case_id != session.state.case_id:
        return _error(
            f"this session is working case {session.state.case_id}, not {case_id!r}",
            case_id=session.state.case_id,
        )
    return None


def _check_hour(at_hour: int, session: Session) -> str | None:
    if not isinstance(at_hour, int) or isinstance(at_hour, bool):
        return _error(f"at_hour must be a whole number of hours, got {at_hour!r}")
    if not 0 <= at_hour < session.params.window_h:
        return _error(
            f"at_hour must be within the recovery window, 0 to {session.params.window_h - 1}",
            at_hour=at_hour,
        )
    if at_hour < session.state.hour:
        return _error(
            f"at_hour {at_hour} is in the past; the case is at hour {session.state.hour}",
            current_hour=session.state.hour,
        )
    return None


def _decision_payload(decision: Decision) -> dict:
    """Every rule's verdict, not just the objecting one.

    The engine does not short-circuit, so the model can see which rules had no opinion
    as well as which refused -- and the ledger records the same set.
    """
    denial = decision.first_denial()
    payload: dict = {
        "allowed": decision.allowed,
        "verdicts": [
            {"rule_id": v.rule_id, "allow": v.allow, "reason": v.reason}
            for v in decision.verdicts
        ],
    }
    if denial is not None:
        payload["rule_id"] = denial.rule_id
        payload["reason"] = denial.reason
        payload["evidence"] = denial.evidence
        payload["all_denials"] = [v.rule_id for v in decision.denials()]
    return payload


def _notice_metadata(session: Session, template: str, debit_at_hour: int) -> dict:
    """Regulatory metadata, filled in by the system rather than by the model.

    The four pre-debit fields are a legal requirement on the notice's *contents*, not a
    judgement call, so they come from the case record. Letting the model type them
    would make a hallucinated amount into a compliance failure.

    Note what is deliberately absent: there is no way for any tool to set
    ``afa_completed``. Additional-factor authentication is something the *customer*
    performs; a recovery system that could assert it on their behalf would be
    self-certifying its way past RBIAdditionalFactorAuth. So AFA-required debits are
    denied for every arm in this project, and contact is the only route on them.
    """
    if template == TEMPLATE_PRE_DEBIT_NOTICE:
        state = session.state
        debit_at = state.failed_at.replace(microsecond=0) + timedelta(hours=debit_at_hour)
        return {
            "merchant_name": MERCHANT_NAME,
            "amount_paise": state.amount_paise,
            "debit_datetime": debit_at.isoformat(),
            "mandate_reference": f"mandate-{state.case_id}",
        }
    if template == TEMPLATE_POST_DEBIT_NOTICE:
        return {"grievance_contact": GRIEVANCE_CONTACT}
    return {}


def _build_action(
    session: Session,
    *,
    action_type: str,
    at_hour: int,
    idempotency_key: str,
    channel: str = "",
    template: str = "",
    debit_at_hour: int = -1,
) -> Action | str:
    """An :class:`Action`, or an error string explaining why it could not be built."""
    if action_type == ACTION_RETRY:
        return Action(
            type=ACTION_RETRY,
            case_id=session.state.case_id,
            at_hour=at_hour,
            idempotency_key=session.namespaced(idempotency_key),
        )

    if action_type != ACTION_MESSAGE:
        return _error(
            f"unknown action_type {action_type!r}", expected=[ACTION_RETRY, ACTION_MESSAGE]
        )

    if channel not in CHANNELS:
        return _error(f"unknown channel {channel!r}", expected=list(CHANNELS))
    if template not in TEMPLATES:
        return _error(f"unknown template {template!r}", expected=list(TEMPLATES))
    if template == TEMPLATE_PRE_DEBIT_NOTICE and debit_at_hour < 0:
        return _error(
            "a pre_debit_notice must say which debit it announces; pass debit_at_hour",
            template=template,
        )

    return Action(
        type=ACTION_MESSAGE,
        case_id=session.state.case_id,
        at_hour=at_hour,
        idempotency_key=session.namespaced(idempotency_key),
        channel=channel,
        template=template,
        metadata=_notice_metadata(session, template, debit_at_hour),
    )


# -- the tools ------------------------------------------------------------------------


@beta_tool
def get_case_detail(case_id: str) -> str:
    """Look up everything known about a failed payment.

    Returns the decline reason and its class, the amount in paise, the payment method,
    the mandate details, and the attempts and contacts made so far.

    Args:
        case_id: The case to look up.
    """
    session = current()
    if (bad := _wrong_case(case_id, session)) is not None:
        return bad

    from recovery.declines import lookup

    state = session.state
    reason = lookup(state.decline_reason)
    detail = state.to_dict()
    detail["decline_class"] = reason.klass.value
    detail["min_retry_wait_h"] = reason.min_retry_wait_h
    # ``is_retryable`` is the taxonomy's own answer. This line used to re-derive it as
    # ``retry_success_base > 0.0``, next to a ``reason.description`` that has never
    # existed on ``DeclineReason`` -- so the first tool call of the first case raised
    # AttributeError. Nothing caught it because the arm had never been run: the tests
    # mock the model asking for this tool, not the tool answering. See WHAT_BROKE.md
    # incident 9.
    detail["retryable"] = reason.is_retryable
    detail["window_h"] = session.params.window_h
    detail["spent_so_far_paise"] = session.provisional.spend(state.case_id)
    return json.dumps(detail, sort_keys=True)


@beta_tool
def get_customer_history(customer_id: str) -> str:
    """Look up what this customer has already been sent and what has been spent on them.

    Contact fatigue is real and cumulative: each prior nudge makes the next one work
    less well, and the ContactFrequency rule caps how many they may receive.

    Args:
        customer_id: The customer to look up.
    """
    session = current()
    state = session.state
    if customer_id != state.customer_id:
        return _error(
            f"this session is working customer {state.customer_id}, not {customer_id!r}",
            customer_id=state.customer_id,
        )

    hours = sorted(session.provisional.contact_hours(customer_id))
    return json.dumps(
        {
            "customer_id": customer_id,
            "segment": state.segment,
            "contacts_this_run": len(hours),
            "contact_hours": hours,
            "contacts_on_this_case": [c.to_dict() for c in state.contacts],
            "attempts_on_this_case": [a.to_dict() for a in state.attempts],
            "spent_on_this_case_paise": session.provisional.spend(state.case_id),
        },
        sort_keys=True,
    )


@beta_tool
def check_policy(
    case_id: str,
    action_type: str,
    at_hour: int,
    channel: str = "",
    template: str = "",
    debit_at_hour: int = -1,
) -> str:
    """Dry-run an action against the policy rules without taking it.

    Free, read-only and side-effect free: nothing is scheduled, no money moves and no
    message is sent. It runs exactly the rules that gate the real action, so use it to
    find an hour that works instead of discovering the constraints by being refused.
    Every rule's verdict comes back, not just the first objection.

    Args:
        case_id: The case the action would apply to.
        action_type: Either "retry" or "message".
        at_hour: Hour offset from the failure at which the action would happen.
        channel: For a message: "sms", "whatsapp" or "email".
        template: For a message: "pre_debit_notice", "post_debit_notice",
            "update_instrument" or "payment_reminder".
        debit_at_hour: For a pre_debit_notice: the hour of the debit it announces.
    """
    session = current()
    if (bad := _wrong_case(case_id, session)) is not None:
        return bad
    if (bad := _check_hour(at_hour, session)) is not None:
        return bad

    built = _build_action(
        session,
        action_type=action_type,
        at_hour=at_hour,
        # A preview key that can never collide with a real one, so a dry run cannot
        # consume the idempotency key the model intends to use for the real action.
        idempotency_key=f"preview:{action_type}:{at_hour}:{channel}:{template}",
        channel=channel,
        template=template,
        debit_at_hour=debit_at_hour,
    )
    if isinstance(built, str):
        return built

    decision = session.gate.preview(built, session.state, index=session.provisional)
    payload = _decision_payload(decision)
    payload["dry_run"] = True
    payload["cost_paise"] = built.cost_paise(session.params.costs)
    return json.dumps(payload, sort_keys=True)


@beta_tool
def schedule_retry(case_id: str, at_hour: int, idempotency_key: str) -> str:
    """Schedule a re-debit attempt on the failed payment.

    The attempt is evaluated against every policy rule before it is accepted. If the
    result says ``"allowed": false``, read ``rule_id`` and ``reason`` and choose
    differently -- repeating the same action will get the same answer.

    Args:
        case_id: The case to retry.
        at_hour: Hour offset from the failure at which to attempt the debit.
        idempotency_key: A short name unique within this case, such as "retry-1". It
            is prefixed with the case id, so the same key never executes twice.
    """
    session = current()
    if (bad := _wrong_case(case_id, session)) is not None:
        return bad
    if (bad := _check_hour(at_hour, session)) is not None:
        return bad
    if (bad := _capacity(session)) is not None:
        return bad
    if not str(idempotency_key).strip():
        return _error("idempotency_key must be a non-empty name unique within this case")

    action = _build_action(
        session, action_type=ACTION_RETRY, at_hour=at_hour, idempotency_key=idempotency_key
    )
    if isinstance(action, str):
        return action
    return _screen_and_queue(session, action)


@beta_tool
def send_message(
    case_id: str,
    channel: str,
    template: str,
    at_hour: int,
    idempotency_key: str,
    debit_at_hour: int = -1,
) -> str:
    """Schedule a message to the customer.

    Evaluated against every policy rule before it is accepted, exactly like a retry.
    The contents of a regulatory notice are filled in from the case record rather than
    by you.

    Args:
        case_id: The case the message is about.
        channel: "sms", "whatsapp" or "email".
        template: One of "payment_reminder" (asks them to pay),
            "update_instrument" (asks them to fix the card or mandate),
            "pre_debit_notice" (the notice an e-mandate debit legally requires
            beforehand) or "post_debit_notice".
        at_hour: Hour offset from the failure at which to send.
        idempotency_key: A short name unique within this case, such as "nudge-1". It
            is prefixed with the case id, so the same key never sends twice.
        debit_at_hour: Required for a pre_debit_notice: the hour of the debit this
            notice announces. It must be at least 24 hours after at_hour.
    """
    session = current()
    if (bad := _wrong_case(case_id, session)) is not None:
        return bad
    if (bad := _check_hour(at_hour, session)) is not None:
        return bad
    if (bad := _capacity(session)) is not None:
        return bad
    if not str(idempotency_key).strip():
        return _error("idempotency_key must be a non-empty name unique within this case")

    action = _build_action(
        session,
        action_type=ACTION_MESSAGE,
        at_hour=at_hour,
        idempotency_key=idempotency_key,
        channel=channel,
        template=template,
        debit_at_hour=debit_at_hour,
    )
    if isinstance(action, str):
        return action
    return _screen_and_queue(session, action)


@beta_tool
def close_case(case_id: str, reason: str) -> str:
    """Stop working this case. Terminal, and the right answer more often than it looks.

    Use it when nothing is worth doing: a hard decline where no retry can ever succeed
    and no message can bring a stolen card back, or a case too small to justify the
    cost of acting. Closing costs nothing.

    Call it *instead of* scheduling, not after. If anything is already scheduled this
    returns an error rather than silently discarding your plan.

    Args:
        case_id: The case to close.
        reason: Why, in a few words. It goes into the audit trail.
    """
    session = current()
    if (bad := _wrong_case(case_id, session)) is not None:
        return bad
    if session.planned:
        return _error(
            f"{len(session.planned)} action(s) are already scheduled on this case; "
            "close_case is for cases where you do nothing at all",
            scheduled=[a.to_dict() for a in session.actions],
        )
    if session.closed:
        return _error("this case is already closed", reason=session.close_reason)

    session.closed = True
    session.close_reason = str(reason).strip() or "no action worthwhile"
    # Queued rather than performed here: closing is not a money or contact action and
    # so is not screened, but it still reaches the transport through Gate.execute at
    # the hour it fires, which is what writes it to the audit trail.
    session.planned.append(
        Action(
            type=ACTION_CLOSE,
            case_id=session.state.case_id,
            at_hour=session.state.hour,
            idempotency_key=session.namespaced("close"),
            metadata={"reason": session.close_reason},
        )
    )
    return json.dumps(
        {"ok": True, "closed": True, "case_id": case_id, "reason": session.close_reason},
        sort_keys=True,
    )


def _capacity(session: Session) -> str | None:
    if session.closed:
        return _error("this case has been closed; no further actions can be scheduled")
    if len(session.planned) >= MAX_PLANNED_ACTIONS:
        return _error(
            f"a plan may hold at most {MAX_PLANNED_ACTIONS} actions and this one is full",
            scheduled=len(session.planned),
        )
    return None


def _screen_and_queue(session: Session, action: Action) -> str:
    """Gate first, queue second. There is no path to the transport from here.

    ``Gate.screen`` evaluates against the provisional history and writes a
    ``policy_check`` entry on refusal -- so a denial reaches the ledger even though
    nothing was attempted. The approval is provisional: the action is gated again by
    ``Gate.execute`` at the hour it actually fires, against the real state.
    """
    decision = session.gate.screen(action, session.state, index=session.provisional)
    payload = _decision_payload(decision)
    payload["scheduled"] = decision.allowed
    payload["at_hour"] = action.at_hour
    payload["idempotency_key"] = action.idempotency_key
    payload["cost_paise"] = action.cost_paise(session.params.costs)

    if decision.allowed:
        session.record(action)
    else:
        session.denials += 1
    return json.dumps(payload, sort_keys=True)


TOOLS = tuple(
    sorted(
        (
            get_case_detail,
            get_customer_history,
            check_policy,
            schedule_retry,
            send_message,
            close_case,
        ),
        key=lambda t: t.name,
    )
)
"""The tool list, **sorted by name**.

Tools render first in the prompt-cache prefix, so a list built in a different order
between two requests would invalidate the cache for everything after it -- the system
prompt included -- and quietly make every call full price.
"""
