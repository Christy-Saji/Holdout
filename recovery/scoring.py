"""The system's *estimate* of recovery probability. It does not know the answer.

The simulator knows the truth; this module guesses. Those being different is what
makes the evaluation honest. A scorer that read ``Case.latents`` would produce an agent
with oracle powers and a headline number that means nothing -- so this module takes a
:class:`~recovery.transport.base.CaseState`, which structurally cannot carry latents,
and never imports ``Latents`` at all.

**Where it is deliberately wrong**, and therefore where the agent's economics are
wrong in the same direction:

1. It assumes every customer has ``ASSUMED_INTENT`` willingness to pay. The simulator
   draws a real per-customer ``intent_to_pay`` that this module cannot see, so the
   scorer is systematically optimistic about reluctant customers and pessimistic about
   eager ones.
2. Its attempt-decay curve is close to, but not the same as, the simulator's. Nobody
   knows the true curve; assuming we had recovered it exactly would be the tell of a
   model fitted to its own data.
3. It has no payroll-cycle term. The simulator gives ``insufficient_funds`` a real
   month-boundary bump; the scorer flattens it. So the scorer *undervalues* well-timed
   retries -- which means the timing advantage the agent arm can find is not one the
   cost rules handed it.

Those gaps are recorded rather than hidden, and they are the kind of thing that belongs
in ``WHAT_BROKE.md`` rather than in a footnote.

All money is integer paise.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from recovery.declines import DeclineClass, lookup
from recovery.transport.base import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CHANNEL_WHATSAPP,
    CaseState,
    SimParams,
)

ASSUMED_INTENT = 0.75
"""Our stand-in for the unobservable per-customer willingness to pay."""

SCORER_ATTEMPT_DECAY = (1.00, 0.42, 0.22, 0.11, 0.05, 0.02, 0.008)
"""Deliberately not the simulator's curve. Close enough to be useful, different enough
that the scorer is not secretly reading the generative model."""

SCORER_RAMP_H = 48.0
SCORER_RAMP_FLOOR = 0.60

APPROVAL_DEGRADATION_COST_PAISE = 700
"""The cost of a *failed* retry beyond its fee.

The build plan's own reasoning for the hard-decline ban is that retrying "burns a fee
**and** trips issuer fraud heuristics, degrading the approval rate on your good
transactions". That second cost is real for soft declines too, just smaller, and if it
is left out of the marginal comparison then a Rs 3 retry against a Rs 600 median amount
is so cheap that the attempt cap barely binds inside a 14-day window. Our own prior,
swept in phase 6 alongside the other cost assumptions.
"""

ASSUMED_CONTACT_RESPONSE = 0.22
"""Base probability that a well-timed nudge converts, before channel and fatigue."""

CHANNEL_PRIOR = {CHANNEL_WHATSAPP: 1.00, CHANNEL_SMS: 0.85, CHANNEL_EMAIL: 0.55}
CONTACT_FATIGUE_BASE = 0.80

QUIET_START_H = 21
QUIET_END_H = 9
QUIET_PENALTY = 0.35


@dataclass
class Scorer:
    """P(recover | intervention) and the expected-value arithmetic built on it."""

    params: SimParams = field(default_factory=SimParams)
    assumed_intent: float = ASSUMED_INTENT
    attempt_decay: tuple[float, ...] = SCORER_ATTEMPT_DECAY

    # -- retries -------------------------------------------------------------------

    def decay(self, attempt_no: int) -> float:
        if attempt_no < 1:
            raise ValueError(f"attempt_no is 1-indexed, got {attempt_no}")
        if attempt_no <= len(self.attempt_decay):
            return self.attempt_decay[attempt_no - 1]
        return self.attempt_decay[-1] * (0.30 ** (attempt_no - len(self.attempt_decay)))

    def p_retry_success(self, state: CaseState, attempt_no: int, at_hour: int) -> float:
        """Estimated P(a retry at ``at_hour`` succeeds).

        Zero for HARD and ACTION by construction, and zero inside the cooling window --
        both facts are public knowledge from the taxonomy, not privileged information.
        """
        reason = lookup(state.decline_reason)  # raises on an unknown code, by design
        if reason.klass is not DeclineClass.SOFT:
            return 0.0
        if at_hour < reason.min_retry_wait_h:
            return 0.0

        elapsed = at_hour - reason.min_retry_wait_h
        ramp = min(1.0, SCORER_RAMP_FLOOR + (1.0 - SCORER_RAMP_FLOOR) * (elapsed / SCORER_RAMP_H))
        return reason.retry_success_base * ramp * self.decay(attempt_no) * self.assumed_intent

    def marginal_retry_cost_paise(self, state: CaseState, attempt_no: int, at_hour: int) -> int:
        """Fee plus the expected issuer-goodwill cost of a retry that fails."""
        p = self.p_retry_success(state, attempt_no, at_hour)
        return int(
            round(self.params.costs.retry_paise + (1.0 - p) * APPROVAL_DEGRADATION_COST_PAISE)
        )

    def marginal_expected_recovery_paise(
        self, state: CaseState, attempt_no: int, at_hour: int
    ) -> int:
        return int(round(self.p_retry_success(state, attempt_no, at_hour) * state.amount_paise))

    # -- contacts ------------------------------------------------------------------

    def p_contact_response(self, state: CaseState, channel: str, at_hour: int) -> float:
        prior = CHANNEL_PRIOR.get(channel)
        if prior is None:
            raise ValueError(f"unknown channel {channel!r}")
        ist_hour = (state.failed_at.hour + at_hour) % 24
        quiet = (
            1.0
            if channel == CHANNEL_EMAIL or QUIET_END_H <= ist_hour < QUIET_START_H
            else QUIET_PENALTY
        )
        fatigue = CONTACT_FATIGUE_BASE ** len(state.contacts)
        return ASSUMED_CONTACT_RESPONSE * prior * quiet * fatigue * self.assumed_intent

    # -- the case as a whole ---------------------------------------------------------

    def p_recover_with_intervention(self, state: CaseState) -> float:
        """Estimated P(this case is recoverable by anything we are allowed to do).

        HARD is zero: no retry works and no message changes the fact that the
        instrument is gone. ACTION is contact-only. SOFT combines the next retry with
        a nudge.
        """
        reason = lookup(state.decline_reason)
        if reason.klass is DeclineClass.HARD:
            return 0.0

        best_contact = max(
            self.p_contact_response(state, channel, max(state.hour, 1))
            for channel in CHANNEL_PRIOR
        )
        if reason.klass is DeclineClass.ACTION:
            return best_contact

        next_retry = self.p_retry_success(
            state, len(state.attempts) + 1, max(state.hour, reason.min_retry_wait_h)
        )
        return 1.0 - (1.0 - next_retry) * (1.0 - best_contact)

    def expected_recovery_paise(self, state: CaseState) -> int:
        """The budget ceiling for a case.

        Spending more than this on a case is a loss dressed as a win, so the
        ``BudgetCeiling`` rule refuses to cross it.
        """
        return int(round(state.amount_paise * self.p_recover_with_intervention(state)))
