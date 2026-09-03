"""The nine operational rules.

Each returns a :class:`~recovery.policy.engine.Verdict` carrying the values it actually
compared, so a denial in the ledger is evidence rather than an assertion. Rules that do
not apply to an action still return a verdict, because the ledger should show that the
whole rule set was consulted.

Every threshold here is our own prior except where it derives from the decline taxonomy
(``min_retry_wait_h``) or from the economics (``AttemptCap``, ``BudgetCeiling``). None
of them reads a latent: a rule receives a ``CaseState``, which has no field to read.
"""

from __future__ import annotations

from recovery.declines import DeclineClass, lookup
from recovery.policy.engine import (
    NOTICE_TEMPLATES,
    PolicyContext,
    Verdict,
    not_applicable,
)
from recovery.transport.base import (
    ACTION_MESSAGE,
    ACTION_RETRY,
    CHANNEL_EMAIL,
    STATUS_OPEN,
    Action,
)

MIN_ATTEMPT_SPACING_H = 12
"""A floor under the gap between consecutive retries, on top of the reason's own
cooling window. ``gateway_timeout`` opens after an hour, but a dozen attempts in a
morning reads as a retry storm to an issuer regardless of what caused the first
failure."""

CONTACT_WINDOW_H = 168  # 7 days
MAX_CONTACTS_PER_WINDOW = 3
MIN_CONTACT_SPACING_H = 24
"""Fatigue is a real cost, not a free action. Three nudges a week, never two in a day."""

QUIET_START_H = 21
QUIET_END_H = 9
"""TRAI-aligned. Dunning to Indian consumers is its own regulated surface, separate
from the RBI mandate rules."""

QUIET_HOURS_CHANNELS = frozenset({"sms", "whatsapp"})


class TerminalState:
    """No action on a recovered, closed or refunded case. The bug that dunts someone
    who already paid."""

    rule_id = "TerminalState"
    summary = "No action on a case that has already recovered or been closed."

    def check(self, action: Action, ctx: PolicyContext) -> Verdict:
        status = ctx.state.status
        allow = status == STATUS_OPEN
        return Verdict(
            rule_id=self.rule_id,
            allow=allow,
            reason="case is open" if allow else f"case is {status}",
            evidence={"status": status, "recovered_at_hour": ctx.state.recovered_at_hour},
        )


class Idempotency:
    """The same ``(case, action, key)`` can never execute twice.

    The key is the *identity* of an action, which is what makes retry-after-timeout
    safe: the caller reuses the key, and a network failure between request and response
    cannot become a second debit.
    """

    rule_id = "Idempotency"
    summary = "An idempotency key executes at most once."

    def check(self, action: Action, ctx: PolicyContext) -> Verdict:
        used = action.idempotency_key in ctx.history.used_keys
        return Verdict(
            rule_id=self.rule_id,
            allow=not used,
            reason="key unused" if not used else "idempotency key already executed",
            evidence={"idempotency_key": action.idempotency_key, "already_used": used},
        )


class HardDeclineNoRetry:
    """Never retry a HARD-class decline.

    Retrying a stolen or closed instrument burns a fee *and* trips issuer fraud
    heuristics, degrading the approval rate on the merchant's good transactions. The
    second cost is the larger one and it does not appear on any invoice.
    """

    rule_id = "HardDeclineNoRetry"
    summary = "Never retry a hard decline (stolen card, closed account, invalid VPA)."

    def check(self, action: Action, ctx: PolicyContext) -> Verdict:
        if action.type != ACTION_RETRY:
            return not_applicable(self.rule_id, "not a retry")
        # lookup() raises UnknownDeclineReason on an unrecognised code. The engine must
        # not catch it: silently treating an unknown reason as soft is how a dunning
        # system starts retrying stolen cards.
        reason = lookup(ctx.state.decline_reason)
        allow = reason.klass is not DeclineClass.HARD
        return Verdict(
            rule_id=self.rule_id,
            allow=allow,
            reason=(
                "decline is not hard-class"
                if allow
                else f"{reason.code} is a permanent decline; retrying cannot succeed"
            ),
            evidence={"decline_reason": reason.code, "klass": reason.klass.value},
        )


class ActionDeclineNoRetry:
    """Never silently retry an ACTION-class decline.

    An expired card or a revoked mandate cannot be fixed by trying again; the
    instrument needs the customer to change something. Contact is the only move.
    """

    rule_id = "ActionDeclineNoRetry"
    summary = "Never silently retry an action-required decline; contact instead."

    def check(self, action: Action, ctx: PolicyContext) -> Verdict:
        if action.type != ACTION_RETRY:
            return not_applicable(self.rule_id, "not a retry")
        reason = lookup(ctx.state.decline_reason)
        allow = reason.klass is not DeclineClass.ACTION
        return Verdict(
            rule_id=self.rule_id,
            allow=allow,
            reason=(
                "decline does not require customer action"
                if allow
                else f"{reason.code} needs the customer to act; a silent retry cannot succeed"
            ),
            evidence={"decline_reason": reason.code, "klass": reason.klass.value},
        )


class CoolingOff:
    """Enforce the reason's own cooling window, and a floor under attempt spacing.

    For ``insufficient_funds`` the window is 24 hours, so a second attempt lands no
    earlier than 48 -- inside the 24-72 hour band where Indian salary accounts refill.
    That band is a consequence of the taxonomy's own number rather than a constant
    invented here, which is why it can be defended rather than merely stated.
    """

    rule_id = "CoolingOff"
    summary = "Respect each reason's cooling window and never bunch retries."

    def check(self, action: Action, ctx: PolicyContext) -> Verdict:
        if action.type != ACTION_RETRY:
            return not_applicable(self.rule_id, "not a retry")

        reason = lookup(ctx.state.decline_reason)
        attempts = ctx.state.attempts
        last_hour = attempts[-1].hour if attempts else None
        since_last = None if last_hour is None else action.at_hour - last_hour
        spacing = max(reason.min_retry_wait_h, MIN_ATTEMPT_SPACING_H)

        evidence = {
            "decline_reason": reason.code,
            "hours_since_failure": action.at_hour,
            "min_retry_wait_h": reason.min_retry_wait_h,
            "hours_since_last_attempt": since_last,
            "required_spacing_h": spacing,
        }

        if action.at_hour < reason.min_retry_wait_h:
            return Verdict(
                rule_id=self.rule_id,
                allow=False,
                reason=(
                    f"{reason.code} cannot succeed for {reason.min_retry_wait_h}h; "
                    f"only {action.at_hour}h have elapsed"
                ),
                evidence=evidence,
            )
        if since_last is not None and since_last < spacing:
            return Verdict(
                rule_id=self.rule_id,
                allow=False,
                reason=f"last attempt was {since_last}h ago; {spacing}h spacing required",
                evidence=evidence,
            )
        return Verdict(
            rule_id=self.rule_id, allow=True, reason="cooling window satisfied", evidence=evidence
        )


class AttemptCap:
    """Stop when marginal expected recovery falls below marginal cost.

    **There is no attempt-count constant in this rule**, and that is the point. The
    answer to "why three?" is that it is not three: it is
    ``P(success | attempt n) * amount < cost_of_attempt``, evaluated per case from
    :mod:`recovery.scoring`. A large amount justifies more attempts than a small one,
    which is both economically correct and testable.

    Marginal cost is the fee plus the expected issuer-goodwill damage of a failed
    attempt -- see ``APPROVAL_DEGRADATION_COST_PAISE``. Without that term a Rs 3 retry
    against a typical amount is so cheap that the cap barely binds inside the window,
    which would be an honest result and a useless rule.
    """

    rule_id = "AttemptCap"
    summary = "Retry only while expected recovery exceeds the cost of the attempt."

    def check(self, action: Action, ctx: PolicyContext) -> Verdict:
        if action.type != ACTION_RETRY:
            return not_applicable(self.rule_id, "not a retry")

        attempt_no = len(ctx.state.attempts) + 1
        p = ctx.scorer.p_retry_success(ctx.state, attempt_no, action.at_hour)
        expected = ctx.scorer.marginal_expected_recovery_paise(ctx.state, attempt_no, action.at_hour)
        marginal_cost = ctx.scorer.marginal_retry_cost_paise(ctx.state, attempt_no, action.at_hour)
        allow = expected >= marginal_cost

        return Verdict(
            rule_id=self.rule_id,
            allow=allow,
            reason=(
                f"expected recovery {expected}p >= marginal cost {marginal_cost}p"
                if allow
                else f"expected recovery {expected}p < marginal cost {marginal_cost}p"
            ),
            evidence={
                "attempt_no": attempt_no,
                "p_success": round(p, 6),
                "amount_paise": ctx.state.amount_paise,
                "expected_recovery_paise": expected,
                "marginal_cost_paise": marginal_cost,
            },
        )


class ContactFrequency:
    """Max contacts per customer per rolling window, and never two in a day.

    Regulatory notices are exempt: a mandatory pre-debit notification is not marketing
    pressure, and suppressing it to preserve a dunning budget would trade a compliance
    obligation for a conversion.
    """

    rule_id = "ContactFrequency"
    summary = "At most three nudges per customer per week, at least a day apart."

    def check(self, action: Action, ctx: PolicyContext) -> Verdict:
        if action.type != ACTION_MESSAGE:
            return not_applicable(self.rule_id, "not a message")
        if action.template in NOTICE_TEMPLATES:
            return not_applicable(
                self.rule_id, "regulatory notice, not commercial contact", template=action.template
            )

        hours = ctx.history.contact_hours(ctx.state.customer_id)
        in_window = [h for h in hours if 0 <= action.at_hour - h < CONTACT_WINDOW_H]
        last = max(hours) if hours else None
        since_last = None if last is None else action.at_hour - last

        evidence = {
            "customer_id": ctx.state.customer_id,
            "contacts_in_window": len(in_window),
            "window_h": CONTACT_WINDOW_H,
            "cap": MAX_CONTACTS_PER_WINDOW,
            "hours_since_last_contact": since_last,
            "min_spacing_h": MIN_CONTACT_SPACING_H,
        }

        if len(in_window) >= MAX_CONTACTS_PER_WINDOW:
            return Verdict(
                rule_id=self.rule_id,
                allow=False,
                reason=(
                    f"{len(in_window)} contacts already sent to this customer in the last "
                    f"{CONTACT_WINDOW_H}h"
                ),
                evidence=evidence,
            )
        if since_last is not None and since_last < MIN_CONTACT_SPACING_H:
            return Verdict(
                rule_id=self.rule_id,
                allow=False,
                reason=f"last contact was {since_last}h ago; {MIN_CONTACT_SPACING_H}h required",
                evidence=evidence,
            )
        return Verdict(
            rule_id=self.rule_id, allow=True, reason="within contact budget", evidence=evidence
        )


class QuietHours:
    """No SMS or WhatsApp between 21:00 and 09:00 IST.

    TRAI-aligned. Email is not covered: it is read when the recipient chooses rather
    than delivered into their evening. Regulatory notices are exempt for the same
    reason they are exempt from the contact budget -- the debit they announce does not
    move to suit the window.

    Distinct from the simulator's ``quiet_hours_penalty``, which models the *economic*
    consequence of a badly-timed message. This rule forbids the send; that one prices
    it. Both exist on purpose.
    """

    rule_id = "QuietHours"
    summary = "No SMS or WhatsApp 21:00-09:00 IST; email is unrestricted."

    def check(self, action: Action, ctx: PolicyContext) -> Verdict:
        if action.type != ACTION_MESSAGE:
            return not_applicable(self.rule_id, "not a message")
        if action.channel == CHANNEL_EMAIL or action.channel not in QUIET_HOURS_CHANNELS:
            return not_applicable(
                self.rule_id, "channel not covered by the quiet-hours window",
                channel=action.channel,
            )
        if action.template in NOTICE_TEMPLATES:
            return not_applicable(
                self.rule_id, "regulatory notice, not commercial contact", template=action.template
            )

        ist_hour = ctx.clock.hour
        quiet = ist_hour >= QUIET_START_H or ist_hour < QUIET_END_H
        return Verdict(
            rule_id=self.rule_id,
            allow=not quiet,
            reason=(
                f"{ist_hour:02d}:00 IST is inside the quiet window"
                if quiet
                else f"{ist_hour:02d}:00 IST is outside the quiet window"
            ),
            evidence={
                "ist_hour": ist_hour,
                "channel": action.channel,
                "quiet_start_h": QUIET_START_H,
                "quiet_end_h": QUIET_END_H,
            },
        )


class BudgetCeiling:
    """Cumulative spend on a case never exceeds its expected recovery.

    Recovery that costs more than it recovers is a loss dressed as a win. The ceiling
    comes from :mod:`recovery.scoring`, which estimates from observable features only --
    so on a HARD decline the ceiling is zero and the rule refuses to spend anything at
    all, including a message.
    """

    rule_id = "BudgetCeiling"
    summary = "Never spend more on a case than it is expected to recover."

    def check(self, action: Action, ctx: PolicyContext) -> Verdict:
        cost = action.cost_paise(ctx.params.costs)
        if cost == 0:
            return not_applicable(self.rule_id, "action costs nothing")

        spent = ctx.history.spend(action.case_id)
        ceiling = ctx.scorer.expected_recovery_paise(ctx.state)
        would_spend = spent + cost
        allow = would_spend <= ceiling

        return Verdict(
            rule_id=self.rule_id,
            allow=allow,
            reason=(
                f"spend {would_spend}p stays within expected recovery {ceiling}p"
                if allow
                else f"spend {would_spend}p would exceed expected recovery {ceiling}p"
            ),
            evidence={
                "spent_paise": spent,
                "action_cost_paise": cost,
                "would_spend_paise": would_spend,
                "ceiling_paise": ceiling,
                "amount_paise": ctx.state.amount_paise,
            },
        )
