"""Inbound payment-status webhooks, handled as **at-least-once**.

Razorpay -- like every webhook producer worth trusting -- guarantees at-least-once
delivery, not exactly-once. A duplicate arrives when the first delivery's HTTP response
is lost, which is precisely the case where the first delivery *did* take effect. A
recovery system that assumes exactly-once therefore double-counts money on exactly the
occasions it most needs to be right.

So idempotence lives here, keyed on Razorpay's own ``x-razorpay-event-id``, and the
recovery side effect lives in a single place --
:meth:`~recovery.transport.razorpay_test.RazorpayTestTransport.mark_recovered`, which
returns whether it changed anything. A second delivery of the same event:

- does not mark the case recovered a second time,
- adds no money to the ledger, and
- fires **no follow-up action**, because the follow-up hook is called only when the
  delivery actually applied.

The duplicate is still written to the ledger, as an ``observation`` carrying
``applied: false`` and ``amount_paise: 0``. Dropping it silently would leave an audit
trail that cannot answer "did we receive that twice?", which is the first question
asked when a customer disputes a double debit. Recording it with zero money attached
costs one line and answers the question.

Nothing in :mod:`recovery.eval` imports this module. It cannot move a published number.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from recovery.ledger import KIND_OBSERVATION, Ledger

SIGNATURE_HEADER = "x-razorpay-signature"
EVENT_ID_HEADER = "x-razorpay-event-id"

EVENT_PAYMENT_CAPTURED = "payment.captured"
EVENT_PAYMENT_FAILED = "payment.failed"
EVENT_PAYMENT_LINK_PAID = "payment_link.paid"
EVENT_SUBSCRIPTION_CHARGED = "subscription.charged"

RECOVERY_EVENTS = frozenset(
    {EVENT_PAYMENT_CAPTURED, EVENT_PAYMENT_LINK_PAID, EVENT_SUBSCRIPTION_CHARGED}
)
"""The events that mean money arrived. Everything else is recorded and ignored.

An allowlist rather than a denylist, for the same reason unknown decline reasons raise:
a Razorpay event this project has never seen must not be able to mark a case recovered
by accident of naming.
"""


class SignatureRejected(RuntimeError):
    """The webhook body did not match its signature. Never processed, always logged."""


class UnroutableEvent(RuntimeError):
    """The payload carries no ``case_id``, so there is nothing to apply it to."""


@dataclass(frozen=True)
class WebhookEvent:
    """One inbound delivery, after signature verification.

    ``at_hour`` is the **simulated** hour the delivery is being processed at, supplied
    by the caller. It is not read from the payload and never from a wall clock: every
    timestamp in this project is the simulated clock, and a ledger entry stamped with
    ``datetime.now()`` would not survive a reproduction run.
    """

    event_id: str
    event: str
    payload: dict = field(default_factory=dict)
    at_hour: int = 0

    @property
    def case_id(self) -> str:
        """The case this event belongs to, from the notes we set on the way out.

        Razorpay echoes ``notes`` back on the entity, which is what makes an inbound
        event routable at all. Every outbound call in
        :mod:`recovery.transport.razorpay_test` sets ``notes.case_id`` for this reason.
        """
        entity = _entity(self.payload)
        notes = entity.get("notes") or {}
        case_id = notes.get("case_id") or entity.get("reference_id")
        if not case_id:
            raise UnroutableEvent(
                f"{self.event} {self.event_id} carries no notes.case_id; there is no case "
                f"to apply it to"
            )
        return str(case_id)

    @property
    def amount_paise(self) -> int:
        """The amount on the entity, in paise. Razorpay's API is paise-native."""
        amount = _entity(self.payload).get("amount", 0)
        if not isinstance(amount, int) or isinstance(amount, bool):
            raise TypeError(
                f"webhook amount must be integer paise, got {type(amount).__name__} "
                f"{amount!r}; a float here would round money"
            )
        return amount

    @property
    def payment_id(self) -> str | None:
        entity = _entity(self.payload)
        pid = entity.get("id")
        return str(pid) if pid else None


def _entity(payload: Mapping[str, Any]) -> dict:
    """The single entity inside a Razorpay webhook envelope.

    Payloads are shaped ``{"payload": {"payment": {"entity": {...}}}}``, with the middle
    key varying by event. Taking the first entity we find keeps this working across
    ``payment``, ``payment_link`` and ``subscription`` events without a mapping table
    that would have to be kept in step with Razorpay's naming.
    """
    for wrapper in (payload.get("payload") or {}).values():
        if isinstance(wrapper, Mapping) and isinstance(wrapper.get("entity"), Mapping):
            return dict(wrapper["entity"])
    return {}


def verify(body: str, headers: Mapping[str, str], secret: str) -> None:
    """Check the HMAC signature. Raises :class:`SignatureRejected` on a mismatch.

    Uses the ``razorpay`` SDK's own ``Utility`` rather than a hand-rolled HMAC, so the
    comparison stays whatever the SDK says it is -- including its constant-time compare.
    """
    import razorpay  # noqa: PLC0415  (optional at import time; see razorpay_test)

    signature = _header(headers, SIGNATURE_HEADER)
    if not signature:
        raise SignatureRejected(f"missing {SIGNATURE_HEADER}")
    try:
        razorpay.Utility().verify_webhook_signature(body, signature, secret)
    except Exception as exc:  # noqa: BLE001 -- SignatureVerificationError and friends
        raise SignatureRejected(str(exc)) from exc


def parse(body: str, headers: Mapping[str, str], *, at_hour: int) -> WebhookEvent:
    """Build an event from a raw request. **Verify separately, and first.**

    Kept apart from :func:`verify` so that a caller cannot accidentally parse an
    unverified body into something that looks trustworthy.
    """
    payload = json.loads(body)
    event_id = _header(headers, EVENT_ID_HEADER)
    if not event_id:
        raise UnroutableEvent(
            f"missing {EVENT_ID_HEADER}; without it a redelivery cannot be told from a "
            f"second, genuine payment"
        )
    return WebhookEvent(
        event_id=event_id,
        event=str(payload.get("event", "")),
        payload=payload,
        at_hour=at_hour,
    )


def _header(headers: Mapping[str, str], name: str) -> str:
    """Case-insensitive header lookup. HTTP header names are not case-sensitive and
    every framework capitalises them differently."""
    for key, value in headers.items():
        if key.lower() == name:
            return str(value)
    return ""


@dataclass(frozen=True)
class Receipt:
    """What one delivery did. ``applied`` is the only field that means money moved."""

    event_id: str
    event: str
    case_id: str | None
    applied: bool
    duplicate: bool
    amount_paise: int
    reason: str

    def to_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "event": self.event,
            "case_id": self.case_id,
            "applied": self.applied,
            "duplicate": self.duplicate,
            "amount_paise": self.amount_paise,
            "reason": self.reason,
        }


@dataclass
class WebhookReceiver:
    """Deduplicates deliveries, applies the recovery once, writes an observation always.

    ``on_recovered`` is the follow-up hook -- cancel outstanding links, notify, stop the
    dunning sequence. It is called **only when the delivery applied**, which is how "no
    follow-up on a duplicate" becomes a property of the code path rather than a rule
    every caller has to remember.
    """

    transport: Any
    ledger: Ledger
    arm: str
    on_recovered: Callable[[Receipt], None] | None = None
    seen: dict[str, Receipt] = field(default_factory=dict)

    def deliver(self, event: WebhookEvent) -> Receipt:
        previous = self.seen.get(event.event_id)
        if previous is not None:
            receipt = Receipt(
                event_id=event.event_id,
                event=event.event,
                case_id=previous.case_id,
                applied=False,
                duplicate=True,
                amount_paise=0,  # never the amount: this is the double-count guard
                reason=f"duplicate delivery of {event.event_id}; first applied={previous.applied}",
            )
            self._record(receipt, event)
            return receipt

        receipt = self._apply(event)
        self.seen[event.event_id] = receipt
        self._record(receipt, event)
        if receipt.applied and self.on_recovered is not None:
            self.on_recovered(receipt)
        return receipt

    def _apply(self, event: WebhookEvent) -> Receipt:
        case_id = event.case_id  # raises UnroutableEvent rather than guessing
        if event.event not in RECOVERY_EVENTS:
            return Receipt(
                event_id=event.event_id,
                event=event.event,
                case_id=case_id,
                applied=False,
                duplicate=False,
                amount_paise=0,
                reason=f"{event.event} is not a recovery event",
            )

        changed = self.transport.mark_recovered(
            case_id, event.at_hour, payment_id=event.payment_id
        )
        return Receipt(
            event_id=event.event_id,
            event=event.event,
            case_id=case_id,
            applied=changed,
            duplicate=False,
            amount_paise=event.amount_paise if changed else 0,
            reason=(
                "recovery recorded"
                if changed
                else "case had already recovered; no second recovery recorded"
            ),
        )

    def _record(self, receipt: Receipt, event: WebhookEvent) -> None:
        """One ``observation`` per delivery, duplicates included.

        ``cost_paise`` is always 0 -- receiving a webhook costs nothing, and the
        recovered amount is not a cost. It rides in ``result`` where the metrics layer
        will not mistake it for spend.
        """
        state = self.transport.observe(receipt.case_id or "", event.at_hour)
        self.ledger.append(
            ts=state.clock,
            arm=self.arm,
            case_id=receipt.case_id or "",
            kind=KIND_OBSERVATION,
            idempotency_key=event.event_id,
            cost_paise=0,
            result={"webhook": receipt.to_dict(), "event": event.event},
        )

    def applied_paise(self) -> int:
        """Total money credited by webhooks. Duplicates contribute nothing by construction."""
        return sum(r.amount_paise for r in self.seen.values() if r.applied)
