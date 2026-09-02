"""Decline taxonomy for failed recurring payments.

Provenance
----------
Reason codes here are modelled on Razorpay's ``error.reason`` field combined with
card-network decline categories. Razorpay documents reason codes per payment method
and does not publish a single exhaustive enumeration across methods, so **this module
is a documented approximation, not a transcription.** Population weights, cooling-off
windows, retry-success rates and self-heal rates are our own **priors** -- stated
openly here rather than buried, and swept in the phase 6 sensitivity analysis so a
reviewer can see whether the sign of the reported lift survives them.

The one external anchor: insufficient funds is consistently reported as roughly
**44%** of card-not-present issuer declines. Our ``insufficient_funds`` weight is
lower (0.28) because this cohort spans UPI, netbanking, e-mandate and wallet as well
as cards, and those non-card methods carry their own failure modes.

Three classes, not two
----------------------
``SOFT``    transient -- retry, correctly timed.
``HARD``    permanent -- never retry. Retrying burns a fee *and* trips issuer fraud
            heuristics, degrading the approval rate on good transactions.
``ACTION``  the instrument needs the customer to change something. A silent retry can
            never succeed; contact is the only move.

Every HARD and ACTION reason has ``retry_success_base == 0.0``. ACTION reasons keep a
non-zero ``self_heal_rate`` because customers do notice a dead subscription and update
the card unprompted -- that unassisted recovery is exactly what inflates naive
gross-recovery numbers, and it is the mechanism by which the control arm recovers
money without anyone doing anything.

Unknown codes raise. Silently defaulting an unrecognised reason to ``SOFT`` is how a
dunning system starts retrying stolen cards.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class DeclineClass(Enum):
    """How a decline should be responded to, not merely what caused it."""

    SOFT = "soft"
    HARD = "hard"
    ACTION = "action"


class Method(Enum):
    """Payment instrument the failed debit was attempted on."""

    CARD = "card"
    UPI = "upi"
    NETBANKING = "netbanking"
    EMANDATE = "emandate"
    WALLET = "wallet"


class UnknownDeclineReason(KeyError):
    """Raised when a reason code is not in the taxonomy. Never fall back to SOFT."""


@dataclass(frozen=True)
class DeclineReason:
    """One decline reason and the priors that govern how it behaves."""

    code: str
    klass: DeclineClass

    min_retry_wait_h: int
    """Cooling-off hours before a retry can succeed at all. Retrying sooner burns a
    fee for a structurally impossible success."""

    retry_success_base: float
    """Base P(success) for a well-timed *first* retry. Zero for HARD and ACTION."""

    self_heal_rate: float
    """P(recovers inside the 336h window with no intervention whatsoever)."""

    population_weight: float
    """Share of the generated cohort. Our own prior; swept in phase 6."""

    methods: frozenset[Method]
    """Payment methods this reason can occur on."""

    @property
    def is_retryable(self) -> bool:
        """True only for SOFT. HARD and ACTION cannot be fixed by retrying."""
        return self.klass is DeclineClass.SOFT


_ALL = frozenset(Method)
_CARD = frozenset({Method.CARD})
_ACCOUNT = frozenset({Method.NETBANKING, Method.EMANDATE})
_MANDATE = frozenset({Method.EMANDATE, Method.UPI})

_REASONS: tuple[DeclineReason, ...] = (
    # ---- SOFT: transient. Retry, correctly timed. -----------------------------
    DeclineReason(
        code="insufficient_funds",
        klass=DeclineClass.SOFT,
        min_retry_wait_h=24,  # payroll-aligned; see the CoolingOff rule in phase 4
        retry_success_base=0.45,
        self_heal_rate=0.20,
        population_weight=0.28,
        methods=_ALL,
    ),
    DeclineReason(
        code="issuer_unavailable",
        klass=DeclineClass.SOFT,
        min_retry_wait_h=6,
        retry_success_base=0.55,
        self_heal_rate=0.15,
        population_weight=0.10,
        methods=frozenset({Method.CARD, Method.UPI, Method.NETBANKING, Method.EMANDATE}),
    ),
    DeclineReason(
        code="gateway_timeout",
        klass=DeclineClass.SOFT,
        min_retry_wait_h=1,
        retry_success_base=0.60,
        self_heal_rate=0.10,
        population_weight=0.08,
        methods=_ALL,
    ),
    DeclineReason(
        code="do_not_honour",
        klass=DeclineClass.SOFT,
        min_retry_wait_h=24,
        retry_success_base=0.30,
        self_heal_rate=0.08,
        population_weight=0.06,
        methods=frozenset({Method.CARD, Method.EMANDATE}),
    ),
    DeclineReason(
        code="risk_declined",
        klass=DeclineClass.SOFT,
        min_retry_wait_h=12,
        retry_success_base=0.35,
        self_heal_rate=0.10,
        population_weight=0.03,
        methods=frozenset({Method.CARD, Method.UPI, Method.WALLET}),
    ),
    # ---- HARD: permanent. Never retry. ----------------------------------------
    DeclineReason(
        code="invalid_account",
        klass=DeclineClass.HARD,
        min_retry_wait_h=0,
        retry_success_base=0.0,
        self_heal_rate=0.0,
        population_weight=0.05,
        methods=_ACCOUNT,
    ),
    DeclineReason(
        code="invalid_vpa",
        klass=DeclineClass.HARD,
        min_retry_wait_h=0,
        retry_success_base=0.0,
        self_heal_rate=0.0,
        population_weight=0.04,
        methods=frozenset({Method.UPI}),
    ),
    DeclineReason(
        code="stolen_card",
        klass=DeclineClass.HARD,
        min_retry_wait_h=0,
        retry_success_base=0.0,
        self_heal_rate=0.0,
        population_weight=0.03,
        methods=_CARD,
    ),
    DeclineReason(
        code="lost_card",
        klass=DeclineClass.HARD,
        min_retry_wait_h=0,
        retry_success_base=0.0,
        self_heal_rate=0.0,
        population_weight=0.03,
        methods=_CARD,
    ),
    DeclineReason(
        code="account_closed",
        klass=DeclineClass.HARD,
        min_retry_wait_h=0,
        retry_success_base=0.0,
        self_heal_rate=0.0,
        population_weight=0.02,
        methods=frozenset({Method.NETBANKING, Method.EMANDATE, Method.UPI}),
    ),
    # ---- ACTION: the customer must change something. ---------------------------
    # retry_success_base is 0.0 by construction, but self_heal_rate is not: people do
    # update a card when the subscription visibly dies.
    DeclineReason(
        code="expired_card",
        klass=DeclineClass.ACTION,
        min_retry_wait_h=0,
        retry_success_base=0.0,
        self_heal_rate=0.25,
        population_weight=0.10,
        methods=_CARD,
    ),
    DeclineReason(
        code="mandate_revoked",
        klass=DeclineClass.ACTION,
        min_retry_wait_h=0,
        retry_success_base=0.0,
        self_heal_rate=0.08,
        population_weight=0.06,
        methods=_MANDATE,
    ),
    DeclineReason(
        code="mandate_paused",
        klass=DeclineClass.ACTION,
        min_retry_wait_h=0,
        retry_success_base=0.0,
        self_heal_rate=0.15,
        population_weight=0.05,
        methods=_MANDATE,
    ),
    DeclineReason(
        code="authentication_failed",
        klass=DeclineClass.ACTION,
        min_retry_wait_h=0,
        retry_success_base=0.0,
        self_heal_rate=0.30,
        population_weight=0.04,
        methods=frozenset({Method.CARD, Method.NETBANKING}),
    ),
    DeclineReason(
        code="card_not_enrolled",
        klass=DeclineClass.ACTION,
        min_retry_wait_h=0,
        retry_success_base=0.0,
        self_heal_rate=0.12,
        population_weight=0.03,
        methods=_CARD,
    ),
)

REASONS: dict[str, DeclineReason] = {r.code: r for r in _REASONS}

# Import-time invariants. Cheap, and they fail loudly at the only moment a typo in the
# table above is still cheap to fix.
assert len(REASONS) == len(_REASONS), "duplicate decline code in the taxonomy"
assert (
    abs(sum(r.population_weight for r in _REASONS) - 1.0) < 1e-9
), "population weights must sum to 1.0"
assert all(
    r.retry_success_base == 0.0
    for r in _REASONS
    if r.klass in (DeclineClass.HARD, DeclineClass.ACTION)
), "HARD and ACTION reasons must have retry_success_base == 0.0"
assert all(
    reason.methods for reason in _REASONS
), "every reason must be valid on at least one method"


def lookup(code: str) -> DeclineReason:
    """Return the reason for ``code``.

    Raises ``UnknownDeclineReason`` on an unrecognised code. **Never** defaults to
    SOFT: an unknown code quietly treated as retryable is how a dunning system ends up
    retrying a stolen card.
    """
    try:
        return REASONS[code]
    except KeyError:
        raise UnknownDeclineReason(code) from None


def reasons_for_method(method: Method) -> list[DeclineReason]:
    """Reasons that can occur on ``method``, in stable declaration order."""
    return [r for r in _REASONS if method in r.methods]


def klass_of(code: str) -> DeclineClass:
    """Convenience wrapper. Raises ``UnknownDeclineReason`` like ``lookup``."""
    return lookup(code).klass
