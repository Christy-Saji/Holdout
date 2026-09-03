"""Three rules from the RBI Digital Payments -- E-mandate Framework, 2026.

**Provenance.** The framework was notified **21 April 2026** and took effect
immediately. It consolidates the prior e-mandate circulars and makes **acquirers
responsible for their merchants' compliance** -- which is to say a payments processor
is on the hook for whether its merchants send the notices, not merely the merchants
themselves. That is why a rule which nominally binds a merchant belongs in a processor's
recovery system.

The citation is repeated at each rule, next to the constant it justifies, so a reviewer
skimming the file hits the source beside the number rather than having to scroll back
to this docstring.

**Scope, stated openly.** ``RBIPreDebitNotice`` is encoded for **e-mandate** debits,
which is what the framework's pre-debit notification requirement names. Recurring
card-on-file debits and UPI Autopay mandates carry analogous notification duties under
their own rails' rules; this project does not encode those, and the rule reports itself
as not applicable rather than pretending the coverage is complete.

All thresholds are integer paise. ``15000.0`` rupees would compare correctly *most* of
the time, which is worse than being wrong every time.
"""

from __future__ import annotations

from datetime import timedelta

from recovery.policy.engine import PolicyContext, Verdict, not_applicable
from recovery.transport.base import (
    ACTION_MESSAGE,
    ACTION_RETRY,
    TEMPLATE_POST_DEBIT_NOTICE,
    TEMPLATE_PRE_DEBIT_NOTICE,
    Action,
)
from recovery.declines import Method

CITATION = "RBI Digital Payments -- E-mandate Framework, notified 21 April 2026"

# -- pre-debit notification ----------------------------------------------------------

PRE_DEBIT_NOTICE_LEAD = timedelta(hours=24)
"""RBI E-mandate Framework 2026: an e-mandate debit requires notification to the
customer **at least 24 hours before** the debit is attempted."""

PRE_DEBIT_NOTICE_REQUIRED_FIELDS = frozenset(
    {"merchant_name", "amount_paise", "debit_datetime", "mandate_reference"}
)
"""RBI E-mandate Framework 2026: the pre-debit notification must carry the merchant's
name, the amount, the date and time of the debit, and the mandate reference. A notice
missing any of these is not a notice."""

PRE_DEBIT_SCOPE_METHODS = frozenset({Method.EMANDATE})
"""Scoped to e-mandate debits -- see the module docstring on what is deliberately not
covered here."""

# -- additional factor of authentication ---------------------------------------------

AFA_STANDARD_THRESHOLD_PAISE = 1_500_000
"""RBI E-mandate Framework 2026: AFA is required above **Rs 15,000** per debit."""

AFA_ELEVATED_THRESHOLD_PAISE = 10_000_000
"""RBI E-mandate Framework 2026: the threshold rises to **Rs 1,00,000** for insurance
premiums, mutual-fund subscriptions and credit-card bill payments."""

AFA_ELEVATED_CATEGORIES = frozenset({"insurance", "mutual_fund", "credit_card_bill"})


class RBIPreDebitNotice:
    """An e-mandate debit requires a complete notification at least 24h beforehand.

    Source: RBI Digital Payments -- E-mandate Framework, notified 21 April 2026.

    The comparison is between **timestamps**, not hour indices, so the 24-hour boundary
    is exact rather than rounded to the simulator's hourly grid -- and it is the
    simulated clock on both sides, never wall-clock time.
    """

    rule_id = "RBIPreDebitNotice"
    citation = CITATION
    summary = (
        "E-mandate debits need a complete pre-debit notice at least 24h prior "
        "(merchant name, amount, debit date/time, mandate reference)."
    )

    def check(self, action: Action, ctx: PolicyContext) -> Verdict:
        if action.type != ACTION_RETRY:
            return not_applicable(self.rule_id, "not a debit")
        if ctx.state.method not in PRE_DEBIT_SCOPE_METHODS:
            return not_applicable(
                self.rule_id,
                "rule is scoped to e-mandate debits",
                method=ctx.state.method.value,
            )

        debit_ts = ctx.clock
        candidates = ctx.history.notices_for(action.case_id, TEMPLATE_PRE_DEBIT_NOTICE)

        best_lead = None
        best_missing: set[str] | None = None
        for notice in candidates:
            lead = debit_ts - notice["sent_at"]
            missing = PRE_DEBIT_NOTICE_REQUIRED_FIELDS - {
                k for k, v in notice["metadata"].items() if v not in (None, "", [])
            }
            if lead >= PRE_DEBIT_NOTICE_LEAD and not missing:
                return Verdict(
                    rule_id=self.rule_id,
                    allow=True,
                    reason=(
                        f"complete pre-debit notice sent "
                        f"{lead.total_seconds() / 3600:.2f}h before the debit"
                    ),
                    evidence={
                        "lead_hours": round(lead.total_seconds() / 3600, 4),
                        "required_lead_hours": PRE_DEBIT_NOTICE_LEAD.total_seconds() / 3600,
                        "notice_sent_at": notice["sent_at"].isoformat(),
                        "debit_at": debit_ts.isoformat(),
                        "missing_fields": [],
                        "citation": CITATION,
                    },
                )
            if best_lead is None or lead > best_lead:
                best_lead = lead
                best_missing = missing

        evidence = {
            "notices_found": len(candidates),
            "required_lead_hours": PRE_DEBIT_NOTICE_LEAD.total_seconds() / 3600,
            "best_lead_hours": (
                None if best_lead is None else round(best_lead.total_seconds() / 3600, 4)
            ),
            "missing_fields": sorted(best_missing) if best_missing else [],
            "debit_at": debit_ts.isoformat(),
            "citation": CITATION,
        }
        if not candidates:
            reason = "no pre-debit notice was sent for this e-mandate debit"
        elif best_missing:
            reason = f"pre-debit notice is missing {sorted(best_missing)}"
        else:
            reason = (
                f"pre-debit notice sent only {evidence['best_lead_hours']}h before the debit; "
                "24h required"
            )
        return Verdict(rule_id=self.rule_id, allow=False, reason=reason, evidence=evidence)


class RBIAdditionalFactorAuth:
    """AFA above the amount threshold, **and on the first debit under a mandate**.

    Source: RBI Digital Payments -- E-mandate Framework, notified 21 April 2026.

    The amount thresholds are the memorable half. The first-debit requirement is the
    half that gets dropped from summaries, and it is not an amount rule at all: a
    one-rupee first debit under a new mandate needs AFA exactly as a one-lakh one does.
    """

    rule_id = "RBIAdditionalFactorAuth"
    citation = CITATION
    summary = (
        "AFA required above Rs 15,000 (Rs 1,00,000 for insurance, mutual funds and "
        "credit-card bills) and on the first debit under a mandate at any amount."
    )

    def check(self, action: Action, ctx: PolicyContext) -> Verdict:
        if action.type != ACTION_RETRY:
            return not_applicable(self.rule_id, "not a debit")

        state = ctx.state
        elevated = state.mandate_category in AFA_ELEVATED_CATEGORIES
        threshold = AFA_ELEVATED_THRESHOLD_PAISE if elevated else AFA_STANDARD_THRESHOLD_PAISE
        over_threshold = state.amount_paise > threshold
        required = state.mandate_first_debit or over_threshold
        completed = bool(action.metadata.get("afa_completed"))

        evidence = {
            "amount_paise": state.amount_paise,
            "mandate_category": state.mandate_category,
            "threshold_paise": threshold,
            "over_threshold": over_threshold,
            "first_debit": state.mandate_first_debit,
            "afa_required": required,
            "afa_completed": completed,
            "citation": CITATION,
        }

        if not required:
            return Verdict(
                rule_id=self.rule_id,
                allow=True,
                reason=f"AFA not required at {state.amount_paise}p on a subsequent debit",
                evidence=evidence,
            )
        if completed:
            return Verdict(
                rule_id=self.rule_id, allow=True, reason="AFA completed", evidence=evidence
            )
        trigger = (
            "first debit under this mandate"
            if state.mandate_first_debit
            else f"amount exceeds the {threshold}p threshold"
        )
        return Verdict(
            rule_id=self.rule_id,
            allow=False,
            reason=f"AFA required ({trigger}) and not completed",
            evidence=evidence,
        )


class RBIPostDebitNotice:
    """A post-transaction notification must carry grievance-redressal details.

    Source: RBI Digital Payments -- E-mandate Framework, notified 21 April 2026.

    A customer who has just been debited under a mandate must be told, in the same
    message, how to complain about it.
    """

    rule_id = "RBIPostDebitNotice"
    citation = CITATION
    summary = "Post-debit notifications must include grievance-redressal details."

    def check(self, action: Action, ctx: PolicyContext) -> Verdict:
        if action.type != ACTION_MESSAGE or action.template != TEMPLATE_POST_DEBIT_NOTICE:
            return not_applicable(self.rule_id, "not a post-debit notification")

        details = str(action.metadata.get("grievance_contact") or "").strip()
        allow = bool(details)
        return Verdict(
            rule_id=self.rule_id,
            allow=allow,
            reason=(
                "grievance-redressal details present"
                if allow
                else "post-debit notification omits grievance-redressal details"
            ),
            evidence={
                "template": action.template,
                "has_grievance_details": allow,
                "citation": CITATION,
            },
        )
