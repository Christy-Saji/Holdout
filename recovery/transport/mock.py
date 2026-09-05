"""The deterministic 336-hour outcome model. This is the only module that reads latents.

Everything here is a pure function of ``(cohort, seed, params, action_sequence)``. Same
inputs, same outcomes, every time, in any process. There is no ``random``, no
``numpy.random`` and no wall clock; every stochastic draw goes through
:func:`recovery.cohort.u`, keyed by the identity of the event.

Why that keying matters, concretely: a retry's outcome is drawn as
``u(seed, case_id, "retry", attempt_no)``, so *attempt one on case #237 uses the same
uniform in the baseline arm and the agent arm*. The two arms differ in **when** they
retry, not in what luck they got. That is the whole variance-reduction argument, and
it is why the confidence interval in phase 6 is as tight as it is.

**The event-kind strings are a frozen interface.** Renaming ``"self_heal"`` to
``"selfheal"`` changes every draw in the project and silently invalidates every
committed result without anything failing.

Every parameter in this file is our own prior unless marked otherwise, and phase 6
sweeps the ones the argument rests on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Iterable, Sequence

from recovery.cohort import Case, u
from recovery.declines import DeclineReason, lookup
from recovery.transport.base import (
    CAUSE_CONTACT,
    CAUSE_RETRY,
    CAUSE_SELF_HEAL,
    CHANNEL_EMAIL,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_RECOVERED,
    Attempt,
    CaseState,
    Contact,
    Result,
    SimParams,
)

# -- outcome-model priors ----------------------------------------------------------

COOLING_RAMP_FLOOR = 0.60
"""A retry taken the moment the cooling window opens is worth 60% of a well-timed one;
it ramps to full effectiveness over the following ``COOLING_RAMP_H`` hours."""
COOLING_RAMP_H = 48.0

STALENESS_FLOOR = 0.70
"""By the end of the 336-hour window a retry is worth 70% of a prompt one -- the
customer has moved on, the subscription has visibly lapsed."""

PAYROLL_BUMP = 1.35
"""Indian salary accounts refill at the month boundary, so an ``insufficient_funds``
retry landing there succeeds materially more often. This is the reason the largest
decline bucket rewards *timing* rather than *volume*, which is the whole thesis of the
agent arm."""
PAYROLL_DAYS_HEAD = 3  # 1st-3rd of the month
PAYROLL_DAYS_TAIL = 28  # 28th onward

TIME_FACTOR_CEILING = 1.40

QUIET_HOURS_PENALTY = 0.35
"""The *economic* consequence of messaging someone at 03:00 -- it simply works less
well. Distinct from the phase 4 ``QuietHours`` policy rule, which forbids the send
outright. Both exist: the rule is the compliance story, this is the economic one."""
QUIET_START_H = 21
QUIET_END_H = 9

FATIGUE_BASE = 0.85
"""Each prior contact to the same customer multiplies the next one's effectiveness.
Fatigue is a real cost, not a free action."""
FATIGUE_FLOOR = 0.15

CONTACT_RESPONSE_WINDOW_H = 48
"""A customer who responds to a nudge pays within two days of it."""


def time_factor(reason: DeclineReason, case: Case, at_hour: int, window_h: int) -> float:
    """How much a retry's base success rate is worth if taken at ``at_hour``.

    **Zero before ``min_retry_wait_h``.** A retry inside the cooling window cannot
    succeed; it only burns a fee and, on a HARD decline, an issuer's patience.
    """
    if at_hour < reason.min_retry_wait_h:
        return 0.0

    elapsed = at_hour - reason.min_retry_wait_h
    ramp = min(1.0, COOLING_RAMP_FLOOR + (1.0 - COOLING_RAMP_FLOOR) * (elapsed / COOLING_RAMP_H))
    staleness = max(STALENESS_FLOOR, 1.0 - (1.0 - STALENESS_FLOOR) * (at_hour / max(1, window_h)))
    factor = ramp * staleness

    if reason.code == "insufficient_funds":
        day = (case.failed_at + timedelta(hours=at_hour)).day
        if day <= PAYROLL_DAYS_HEAD or day >= PAYROLL_DAYS_TAIL:
            factor *= PAYROLL_BUMP

    return min(TIME_FACTOR_CEILING, factor)


def quiet_hours_penalty(ist_hour: int, channel: str) -> float:
    """Email is asynchronous and carries no quiet-hours penalty; SMS and WhatsApp do."""
    if channel == CHANNEL_EMAIL:
        return 1.0
    if ist_hour >= QUIET_START_H or ist_hour < QUIET_END_H:
        return QUIET_HOURS_PENALTY
    return 1.0


def fatigue_penalty(contacts_already_sent: int) -> float:
    return max(FATIGUE_FLOOR, FATIGUE_BASE**contacts_already_sent)


def self_heal_hazard(p_self_heal: float, window_h: int) -> float:
    """Per-hour hazard that integrates to ``p_self_heal`` across the window.

    ``1 - (1 - p) ** (1/W)``, **not** ``p / W``. Getting the root wrong is the single
    likeliest way to end up with a control arm that recovers nothing, which looks like
    a clean result and is a broken one -- the control arm recovering money on its own
    is exactly what makes gross numbers misleading.
    """
    p = min(max(p_self_heal, 0.0), 0.999)
    if p <= 0.0:
        return 0.0
    return 1.0 - (1.0 - p) ** (1.0 / window_h)


# -- per-case mutable simulation state ---------------------------------------------


@dataclass
class _CaseSim:
    case: Case  # carries latents; never leaves this module
    status: str = STATUS_OPEN
    attempts: list[Attempt] = field(default_factory=list)
    contacts: list[Contact] = field(default_factory=list)
    scheduled: list[tuple[int, str]] = field(default_factory=list)  # (hour, cause)
    recovered_at_hour: int | None = None
    recovery_cause: str | None = None
    closed_reason: str | None = None
    cost_paise: int = 0

    @property
    def has_recovered(self) -> bool:
        return self.recovered_at_hour is not None

    @property
    def accepts_actions(self) -> bool:
        """Whether an arm may still act on this case.

        Distinct from :attr:`has_recovered`. Closing a case is an *internal* decision
        to stop working it; it does not reach the customer and cannot stop them
        updating their card of their own accord. So a closed case keeps its self-heal
        schedule and can still recover -- otherwise "give up on this one" would destroy
        real money and an arm would be punished for the correct call on a dead
        instrument.
        """
        return not self.has_recovered and self.closed_reason is None


@dataclass(frozen=True)
class CaseOutcome:
    """What actually happened to one case in one arm. The unit phase 6 bootstraps."""

    case_id: str
    customer_id: str
    amount_paise: int
    recovered: bool
    recovered_at_hour: int | None
    recovery_cause: str | None
    cost_paise: int
    attempts: int
    contacts: int

    @property
    def recovered_paise(self) -> int:
        return self.amount_paise if self.recovered else 0

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "customer_id": self.customer_id,
            "amount_paise": self.amount_paise,
            "recovered": self.recovered,
            "recovered_at_hour": self.recovered_at_hour,
            "recovery_cause": self.recovery_cause,
            "cost_paise": self.cost_paise,
            "attempts": self.attempts,
            "contacts": self.contacts,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "CaseOutcome":
        return cls(
            case_id=d["case_id"],
            customer_id=d["customer_id"],
            amount_paise=d["amount_paise"],
            recovered=d["recovered"],
            recovered_at_hour=d["recovered_at_hour"],
            recovery_cause=d["recovery_cause"],
            cost_paise=d["cost_paise"],
            attempts=d["attempts"],
            contacts=d["contacts"],
        )


class MockTransport:
    """Deterministic simulator implementing :class:`~recovery.transport.base.Transport`.

    One instance per arm. Sharing an instance between arms is the failure mode that
    produces a beautiful, wrong number: arm two would inherit arm one's attempt
    history.
    """

    def __init__(
        self,
        cases: Sequence[Case],
        seed: int,
        params: SimParams | None = None,
    ) -> None:
        self.seed = seed
        self.params = params or SimParams()
        missing = [c.case_id for c in cases if c.latents is None]
        if missing:
            raise ValueError(
                f"the simulator needs latents; {len(missing)} case(s) were loaded without them "
                f"(first: {missing[0]}). Load with with_latents=True."
            )

        # Sorted so that anything iterating the case set has a fixed order. Dict or set
        # iteration order deciding draw order is a latent non-determinism bug.
        self._sims: dict[str, _CaseSim] = {
            c.case_id: _CaseSim(case=c) for c in sorted(cases, key=lambda c: c.case_id)
        }
        self._customer_contacts: dict[str, int] = {}
        self._hour = -1

        for sim in self._sims.values():
            hour = self._self_heal_hour(sim.case)
            if hour is not None:
                sim.scheduled.append((hour, CAUSE_SELF_HEAL))

    # -- self-heal ------------------------------------------------------------------

    def _self_heal_hour(self, case: Case) -> int | None:
        """The hour this case would recover on its own, or ``None`` if it never does.

        The spec describes drawing ``u(seed, case_id, "self_heal", t)`` at each
        untouched hour. Because that draw depends only on ``(seed, case_id, t)`` and
        never on what an arm did, the first hour at which it fires is a pure function
        of the case -- so we resolve it up front. The two formulations are equivalent,
        and this one makes the CRN pairing property exact rather than merely intended:
        an arm *cannot* perturb when a case would have healed.
        """
        assert case.latents is not None
        p = case.latents.p_self_heal * self.params.p_self_heal_scale
        hazard = self_heal_hazard(p, self.params.window_h)
        if hazard <= 0.0:
            return None
        for t in range(self.params.window_h):
            if u(self.seed, case.case_id, "self_heal", t) < hazard:
                return t
        return None

    # -- clock ----------------------------------------------------------------------

    def tick(self, at_hour: int) -> None:
        """Resolve everything scheduled at or before ``at_hour``.

        Called at the top of each simulated hour, before any arm acts, so that an arm
        never acts on a case that has already recovered this hour.
        """
        self._hour = at_hour
        for sim in self._sims.values():
            # Deliberately not gated on ``accepts_actions``: a case we closed can
            # still self-heal, and the counterfactual would be wrong if it could not.
            if sim.has_recovered or not sim.scheduled:
                continue
            due = [(h, cause) for h, cause in sim.scheduled if h <= at_hour]
            if not due:
                continue
            # Earliest event wins; ties resolve by the fixed cause ordering below, so
            # a self-heal and a contact response landing in the same hour resolve
            # identically in every process.
            hour, cause = min(due, key=lambda hc: (hc[0], hc[1]))
            self._recover(sim, hour, cause)

    def _recover(self, sim: _CaseSim, at_hour: int, cause: str) -> None:
        """Terminal is terminal. A case recovers once, at the earliest event."""
        if sim.has_recovered:
            return
        sim.status = STATUS_RECOVERED
        sim.recovered_at_hour = at_hour
        sim.recovery_cause = cause
        sim.scheduled.clear()

    def mark_recovered(
        self,
        case_id: str,
        at_hour: int,
        payment_id: str | None = None,
        cause: str = CAUSE_CONTACT,
    ) -> bool:
        """Record a payment confirmed from outside the simulation. Returns whether it changed anything.

        The inbound counterpart of the outbound actions: a customer paid, and the system
        found out about it rather than causing it. ``RazorpayTestTransport`` offers the
        same method with the same contract so that
        :class:`~recovery.transport.webhooks.WebhookReceiver` -- and the duplicate-delivery
        guarantee that lives in it -- can be exercised offline against the simulator.

        The return value is the whole point. At-least-once webhook delivery means the
        same payment arrives twice; idempotence has to live in **one** place or each
        caller re-derives it and one of them gets it wrong. This is that place: the
        second call returns ``False`` and touches nothing.

        Nothing in :mod:`recovery.eval` calls this, so it cannot move a published number.
        """
        sim = self._sim(case_id)
        if sim.has_recovered:
            return False
        self._recover(sim, at_hour, cause)
        return True

    # -- actions ---------------------------------------------------------------------

    def attempt_retry(self, case_id: str, at_hour: int, idempotency_key: str) -> Result:
        sim = self._sim(case_id)
        if not sim.accepts_actions:
            # The TerminalState rule prevents this in every gated arm; refuse rather
            # than charge, and never let a recovered case recover twice.
            return Result(
                ok=False,
                kind="retry",
                recovered=False,
                cost_paise=0,
                detail={"rejected": "terminal", "status": sim.status},
            )

        attempt_no = len(sim.attempts) + 1
        cost = self.params.costs.retry_paise
        sim.cost_paise += cost

        probability = self._p_retry_success(sim, at_hour, attempt_no)
        succeeded = u(self.seed, case_id, "retry", attempt_no) < probability

        sim.attempts.append(Attempt(hour=at_hour, attempt_no=attempt_no, succeeded=succeeded))
        if succeeded:
            self._recover(sim, at_hour, CAUSE_RETRY)

        # detail carries no probability and no draw: both are functions of the latent
        # intent_to_pay and would leak hidden ground truth into the arm and the ledger.
        return Result(
            ok=True,
            kind="retry",
            recovered=succeeded,
            cost_paise=cost,
            detail={"attempt_no": attempt_no, "at_hour": at_hour, "succeeded": succeeded},
        )

    def _p_retry_success(self, sim: _CaseSim, at_hour: int, attempt_no: int) -> float:
        case = sim.case
        assert case.latents is not None
        reason = lookup(case.decline_reason)
        return (
            case.latents.retry_success_base
            * time_factor(reason, case, at_hour, self.params.window_h)
            * self.params.decay(attempt_no)
            * case.latents.intent_to_pay
        )

    def send_message(
        self,
        case_id: str,
        channel: str,
        template: str,
        at_hour: int,
        idempotency_key: str,
    ) -> Result:
        sim = self._sim(case_id)
        if not sim.accepts_actions:
            return Result(
                ok=False,
                kind="message",
                recovered=False,
                cost_paise=0,
                detail={"rejected": "terminal", "status": sim.status},
            )

        cost = self.params.costs.message_paise(channel)
        sim.cost_paise += cost

        contact_index = len(sim.contacts)
        customer_id = sim.case.customer.customer_id
        already_sent = self._customer_contacts.get(customer_id, 0)

        probability = self._p_contact_response(sim, channel, at_hour, already_sent)
        responded = u(self.seed, case_id, f"contact_{channel}", contact_index) < probability

        sim.contacts.append(Contact(hour=at_hour, channel=channel, template=template))
        self._customer_contacts[customer_id] = already_sent + 1

        if responded:
            delay = 1 + int(
                u(self.seed, case_id, f"contact_delay_{channel}", contact_index)
                * CONTACT_RESPONSE_WINDOW_H
            )
            sim.scheduled.append((at_hour + delay, CAUSE_CONTACT))

        # The send itself never reports recovery: a customer who responds pays later,
        # and the ``responded`` flag is withheld because it is a latent-derived fact.
        return Result(
            ok=True,
            kind="message",
            recovered=False,
            cost_paise=cost,
            detail={"channel": channel, "template": template, "at_hour": at_hour},
        )

    def _p_contact_response(
        self, sim: _CaseSim, channel: str, at_hour: int, already_sent: int
    ) -> float:
        case = sim.case
        assert case.latents is not None
        affinity = case.latents.channel_affinity.get(channel)
        if affinity is None:
            raise ValueError(f"no channel affinity for {channel!r}")
        ist_hour = (case.failed_at + timedelta(hours=at_hour)).hour
        return (
            case.latents.responsiveness
            * affinity
            * quiet_hours_penalty(ist_hour, channel)
            * fatigue_penalty(already_sent)
        )

    def close_case(self, case_id: str, at_hour: int, reason: str) -> Result:
        """Terminal, free, and not a money or contact action -- so it is not gated."""
        sim = self._sim(case_id)
        if not sim.accepts_actions:
            return Result(
                ok=False,
                kind="close",
                recovered=False,
                cost_paise=0,
                detail={"rejected": "terminal", "status": sim.status},
            )
        sim.status = STATUS_CLOSED
        sim.closed_reason = reason
        # The self-heal schedule is intentionally left intact -- see accepts_actions.
        return Result(
            ok=True, kind="close", recovered=False, cost_paise=0, detail={"reason": reason}
        )

    # -- observation ------------------------------------------------------------------

    def observe(self, case_id: str, at_hour: int) -> CaseState:
        """State as of ``at_hour`` and no later. Observation has no side effects."""
        sim = self._sim(case_id)
        case = sim.case

        recovered_at = sim.recovered_at_hour
        if recovered_at is not None and recovered_at <= at_hour:
            status = STATUS_RECOVERED
            visible_recovered_at: int | None = recovered_at
        elif sim.closed_reason is not None:
            status = STATUS_CLOSED
            visible_recovered_at = None
        else:
            status = STATUS_OPEN
            visible_recovered_at = None

        return CaseState(
            case_id=case.case_id,
            customer_id=case.customer.customer_id,
            segment=case.customer.segment,
            payment_id=case.payment_id,
            amount_paise=case.amount_paise,
            method=case.method,
            decline_reason=case.decline_reason,
            failed_at=case.failed_at,
            is_recurring=case.is_recurring,
            mandate_first_debit=case.mandate_first_debit,
            mandate_category=case.mandate_category,
            hour=at_hour,
            status=status,
            attempts=tuple(a for a in sim.attempts if a.hour <= at_hour),
            contacts=tuple(c for c in sim.contacts if c.hour <= at_hour),
            recovered_at_hour=visible_recovered_at,
        )

    # -- results ------------------------------------------------------------------------

    def case_ids(self) -> tuple[str, ...]:
        return tuple(self._sims)

    def outcomes(self) -> tuple[CaseOutcome, ...]:
        """Per-case results after the window closes. For the harness, not for arms."""
        return tuple(
            CaseOutcome(
                case_id=sim.case.case_id,
                customer_id=sim.case.customer.customer_id,
                amount_paise=sim.case.amount_paise,
                recovered=sim.has_recovered,
                recovered_at_hour=sim.recovered_at_hour,
                recovery_cause=sim.recovery_cause,
                cost_paise=sim.cost_paise,
                attempts=len(sim.attempts),
                contacts=len(sim.contacts),
            )
            for sim in self._sims.values()
        )

    def contacts_for_customer(self, customer_id: str) -> int:
        return self._customer_contacts.get(customer_id, 0)

    def _sim(self, case_id: str) -> _CaseSim:
        try:
            return self._sims[case_id]
        except KeyError:
            raise KeyError(f"no such case in this cohort: {case_id!r}") from None


def open_case_ids(transport: MockTransport, at_hour: int) -> Iterable[str]:
    """Case ids still open at ``at_hour``, in sorted order."""
    return (cid for cid in transport.case_ids() if not transport.observe(cid, at_hour).is_terminal)
