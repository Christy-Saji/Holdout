"""The interface every arm codes against, and the vocabulary they share.

``mock.py`` (phase 3) and ``razorpay_test.py`` (phase 7) are interchangeable behind
this Protocol, so an arm cannot tell whether it is driving a simulator or a live test
account.

The seam that matters is :class:`CaseState`. It is what ``observe()`` returns and it
carries **only what an arm is allowed to see** -- status, attempt history, contacts
sent, decline reason, amount. Never latents, and never anything about a future hour.
That makes the hidden ground truth structural rather than conventional: an arm, a
policy rule and the scorer all receive this type and none of them receives a
``Latents``.

All money is integer paise.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from recovery.declines import Method

WINDOW_H = 336
"""The 14-day recovery window, in hours. Every simulation runs over exactly this."""

# -- action and status vocabulary ------------------------------------------------

ACTION_RETRY = "retry"
ACTION_MESSAGE = "message"
ACTION_CLOSE = "close"
ACTION_TYPES = frozenset({ACTION_RETRY, ACTION_MESSAGE, ACTION_CLOSE})

CHANNEL_SMS = "sms"
CHANNEL_WHATSAPP = "whatsapp"
CHANNEL_EMAIL = "email"
CHANNELS = (CHANNEL_SMS, CHANNEL_WHATSAPP, CHANNEL_EMAIL)

STATUS_OPEN = "open"
STATUS_RECOVERED = "recovered"
STATUS_CLOSED = "closed"

CAUSE_SELF_HEAL = "self_heal"
CAUSE_RETRY = "retry"
CAUSE_CONTACT = "contact"

TEMPLATE_PRE_DEBIT_NOTICE = "pre_debit_notice"
TEMPLATE_POST_DEBIT_NOTICE = "post_debit_notice"
TEMPLATE_UPDATE_INSTRUMENT = "update_instrument"
TEMPLATE_PAYMENT_REMINDER = "payment_reminder"


# -- cost priors ------------------------------------------------------------------


@dataclass(frozen=True)
class CostModel:
    """What each action costs the merchant, in paise.

    **These are our own priors, not quoted Razorpay rates**, and phase 6 sweeps them.
    Cost per incremental rupee moves directly with these numbers, so they are a
    dataclass rather than module constants: the sweep needs to vary them.
    """

    retry_paise: int = 300  # Rs 3.00
    sms_paise: int = 20  # Rs 0.20
    whatsapp_paise: int = 35  # Rs 0.35
    email_paise: int = 1  # Rs 0.01

    def message_paise(self, channel: str) -> int:
        try:
            return {
                CHANNEL_SMS: self.sms_paise,
                CHANNEL_WHATSAPP: self.whatsapp_paise,
                CHANNEL_EMAIL: self.email_paise,
            }[channel]
        except KeyError:
            raise ValueError(f"unknown channel {channel!r}; expected one of {CHANNELS}") from None

    def scaled(self, factor: float) -> "CostModel":
        """A cost model with every price scaled. Used by the phase 6 sweep."""
        return CostModel(
            retry_paise=max(0, round(self.retry_paise * factor)),
            sms_paise=max(0, round(self.sms_paise * factor)),
            whatsapp_paise=max(0, round(self.whatsapp_paise * factor)),
            email_paise=max(0, round(self.email_paise * factor)),
        )


# -- simulation parameters (everything phase 6 is allowed to sweep) ----------------

DEFAULT_ATTEMPT_DECAY = (1.00, 0.40, 0.24, 0.10, 0.04, 0.015, 0.005)
"""Marginal success multiplier by attempt number, relative to the first retry.

The published shape: a first retry recovers roughly 40-60% of *recoverable* soft
declines, the second another 15-25%, the third 10-15%. Expressed relative to attempt
one that is 1.00, then ~0.40, then ~0.24, sharply diminishing thereafter. Our own
prior for the tail beyond the third; swept in phase 6.
"""


@dataclass(frozen=True)
class SimParams:
    """Knobs the phase 6 sensitivity sweep varies. Defaults are the published run."""

    window_h: int = WINDOW_H
    p_self_heal_scale: float = 1.0
    """Multiplier on every case's latent ``p_self_heal``. The parameter the whole
    'you invented your own probabilities' critique rests on."""

    attempt_decay: tuple[float, ...] = DEFAULT_ATTEMPT_DECAY
    costs: CostModel = field(default_factory=CostModel)

    def decay(self, attempt_no: int) -> float:
        """Multiplier for a 1-indexed attempt number. Monotonically decreasing."""
        if attempt_no < 1:
            raise ValueError(f"attempt_no is 1-indexed, got {attempt_no}")
        curve = self.attempt_decay
        if attempt_no <= len(curve):
            return curve[attempt_no - 1]
        # Beyond the tabulated curve, keep decaying rather than flat-lining.
        return curve[-1] * (0.30 ** (attempt_no - len(curve)))

    def to_dict(self) -> dict:
        return {
            "window_h": self.window_h,
            "p_self_heal_scale": self.p_self_heal_scale,
            "attempt_decay": list(self.attempt_decay),
            "costs": {
                "retry_paise": self.costs.retry_paise,
                "sms_paise": self.costs.sms_paise,
                "whatsapp_paise": self.costs.whatsapp_paise,
                "email_paise": self.costs.email_paise,
            },
        }


# -- actions ----------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    """A money or contact action an arm wants to take.

    Every instance of this type must pass ``PolicyEngine.evaluate()`` before it
    reaches a transport (phase 4). ``metadata`` carries the fields the RBI rules
    inspect -- ``afa_completed``, the pre-debit notice contents, grievance details.
    """

    type: str
    case_id: str
    at_hour: int
    idempotency_key: str
    channel: str | None = None
    template: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in ACTION_TYPES:
            raise ValueError(f"unknown action type {self.type!r}; expected {sorted(ACTION_TYPES)}")
        if self.type == ACTION_MESSAGE and self.channel not in CHANNELS:
            raise ValueError(f"message action needs a channel, got {self.channel!r}")

    def cost_paise(self, costs: CostModel) -> int:
        if self.type == ACTION_RETRY:
            return costs.retry_paise
        if self.type == ACTION_MESSAGE:
            return costs.message_paise(self.channel or "")
        return 0

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "case_id": self.case_id,
            "at_hour": self.at_hour,
            "idempotency_key": self.idempotency_key,
            "channel": self.channel,
            "template": self.template,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class Result:
    """What a transport reports back. Deliberately thin.

    ``detail`` must never carry a latent-derived quantity -- not the success
    probability the simulator computed, not the uniform it drew. Both are functions of
    ``intent_to_pay`` and would leak hidden ground truth to the arm and into the
    ledger.
    """

    ok: bool
    kind: str
    recovered: bool
    cost_paise: int
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "kind": self.kind,
            "recovered": self.recovered,
            "cost_paise": self.cost_paise,
            "detail": dict(self.detail),
        }


# -- the arm-visible view of a case -------------------------------------------------


@dataclass(frozen=True)
class Attempt:
    hour: int
    attempt_no: int
    succeeded: bool

    def to_dict(self) -> dict:
        return {"hour": self.hour, "attempt_no": self.attempt_no, "succeeded": self.succeeded}


@dataclass(frozen=True)
class Contact:
    hour: int
    channel: str
    template: str

    def to_dict(self) -> dict:
        return {"hour": self.hour, "channel": self.channel, "template": self.template}


@dataclass(frozen=True)
class CaseState:
    """Everything an arm, a policy rule or the scorer may know about a case.

    Note what is absent: ``p_self_heal``, ``retry_success_base``, ``intent_to_pay``,
    ``responsiveness``, ``channel_affinity``, and the cause of a recovery. The first
    five are the latents. The sixth is excluded too -- telling an arm that a case
    recovered *on its own* would leak the counterfactual one case at a time.
    """

    case_id: str
    customer_id: str
    segment: str
    payment_id: str
    amount_paise: int
    method: Method
    decline_reason: str
    failed_at: datetime
    is_recurring: bool
    mandate_first_debit: bool
    mandate_category: str

    hour: int
    status: str
    attempts: tuple[Attempt, ...] = ()
    contacts: tuple[Contact, ...] = ()
    recovered_at_hour: int | None = None

    @property
    def clock(self) -> datetime:
        """The simulated wall clock at ``hour``. IST, because ``failed_at`` is."""
        return self.failed_at + timedelta(hours=self.hour)

    @property
    def hours_since_failure(self) -> int:
        return self.hour

    @property
    def is_terminal(self) -> bool:
        return self.status != STATUS_OPEN

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    @property
    def contact_count(self) -> int:
        return len(self.contacts)

    @property
    def hours_since_last_attempt(self) -> int | None:
        if not self.attempts:
            return None
        return self.hour - self.attempts[-1].hour

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "customer_id": self.customer_id,
            "segment": self.segment,
            "payment_id": self.payment_id,
            "amount_paise": self.amount_paise,
            "method": self.method.value,
            "decline_reason": self.decline_reason,
            "failed_at": self.failed_at.isoformat(),
            "is_recurring": self.is_recurring,
            "mandate_first_debit": self.mandate_first_debit,
            "mandate_category": self.mandate_category,
            "hour": self.hour,
            "status": self.status,
            "attempts": [a.to_dict() for a in self.attempts],
            "contacts": [c.to_dict() for c in self.contacts],
            "recovered_at_hour": self.recovered_at_hour,
        }


# -- the interface ------------------------------------------------------------------


class Transport(Protocol):
    """What an arm is allowed to do to the outside world.

    ``tick`` is a clock-advance hook rather than an action: the simulator uses it to
    resolve events scheduled for the current hour (a self-heal, a delayed response to
    a message). A live transport implements it as a no-op.
    """

    def tick(self, at_hour: int) -> None: ...

    def attempt_retry(self, case_id: str, at_hour: int, idempotency_key: str) -> Result: ...

    def send_message(
        self,
        case_id: str,
        channel: str,
        template: str,
        at_hour: int,
        idempotency_key: str,
    ) -> Result: ...

    def close_case(self, case_id: str, at_hour: int, reason: str) -> Result: ...

    def observe(self, case_id: str, at_hour: int) -> CaseState: ...
