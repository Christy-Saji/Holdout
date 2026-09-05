"""Razorpay **test-mode** transport, behind the same protocol as the simulator.

This implements :class:`~recovery.transport.base.Transport`, so it drops in wherever
``MockTransport`` goes and **no arm changes**. That interchangeability is the point: an
arm cannot tell whether it is driving a simulator or a live test account, which is what
makes the mock a defensible default rather than a convenience.

**None of the published numbers come from here, and that is deliberate.** A network
result is not reproducible and not offline-replayable: two runs a day apart would
differ, the clean-clone gate in phase 8 could not byte-compare anything, and a reviewer
without credentials could not run the project at all. The headline is measured against
the deterministic mock; this module exists to show the wiring is real.

**Honest status: this file has never made a live call.** Razorpay test-mode keys were
not available before the submission date, so ``RAZORPAY_KEY_ID`` is unset and every
test that would exercise it skips. It is written against the ``razorpay`` SDK's
documented surface and is unexercised. That is recorded here, in ``ARCHITECTURE.md``
and in ``WHAT_BROKE.md`` rather than left for a reviewer to discover -- the same
treatment the unrecorded agent arm gets.

Four surfaces are wired, chosen because they are the ones the recovery loop and the RBI
rules actually touch:

======================  =========================================================
``attempt_retry``       Orders API, or ``payments/create/recurring`` for e-mandate
``send_message``        Payment Links -- the contact-to-pay path
``close_case``          cancels any outstanding link; no other remote effect
webhooks                :mod:`recovery.transport.webhooks`, inbound status
======================  =========================================================

Money is integer paise on both sides of this boundary: Razorpay's API is paise-native,
so there is no float rupee conversion anywhere in this file, not even at the edge.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Sequence

from recovery.cohort import Case
from recovery.declines import Method
from recovery.transport.base import (
    CHANNEL_EMAIL,
    CHANNEL_SMS,
    CHANNEL_WHATSAPP,
    STATUS_CLOSED,
    STATUS_OPEN,
    STATUS_RECOVERED,
    Attempt,
    CaseState,
    Contact,
    Result,
    SimParams,
)

KEY_ID_VAR = "RAZORPAY_KEY_ID"
KEY_SECRET_VAR = "RAZORPAY_KEY_SECRET"

TEST_KEY_PREFIX = "rzp_test_"
"""Razorpay test keys are prefixed ``rzp_test_``; live ones ``rzp_live_``. The guard in
:func:`credentials_from_env` refuses a live key outright. A dunning system pointed at a
live account by an environment-variable mistake would send real money movements to real
customers, so this check is cheap insurance against the worst available accident."""

CURRENCY = "INR"

PAYMENT_LINK_EXPIRY_H = 72
"""How long a recovery payment link stays payable. Longer than the 24-72h band in which
``insufficient_funds`` cases refill, short enough that a stale link does not collect a
payment weeks after the case was closed."""

_LINK_NOTIFY_MEDIUM = {CHANNEL_SMS: "sms", CHANNEL_EMAIL: "email"}
"""Payment Links notify over SMS and e-mail. WhatsApp is a separate Razorpay product
with its own onboarding, so this transport reports it unsupported rather than silently
downgrading the channel -- a channel substitution would quietly invalidate any
comparison against a mock run that used WhatsApp."""


class MissingCredentials(RuntimeError):
    """Raised when the test-mode keys are absent, naming the variable that is unset."""


class LiveKeyRefused(RuntimeError):
    """Raised when the configured key is not a test-mode key."""


# -- credentials --------------------------------------------------------------------


def load_env_file(path: Path | str = ".env") -> dict[str, str]:
    """Parse a ``KEY=value`` file. Returns ``{}`` when it does not exist.

    Deliberately hand-rolled rather than pulling in ``python-dotenv``: the project has
    four runtime dependencies and a twelve-line parser is not worth a fifth. Values are
    not expanded and quotes are stripped, which is the whole of the format this project
    uses -- ``.env.example`` is the specification.
    """
    file = Path(path)
    if not file.exists():
        return {}
    out: dict[str, str] = {}
    for line in file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


def credentials_from_env(env: dict[str, str] | None = None) -> tuple[str, str]:
    """``(key_id, key_secret)`` from the process environment, falling back to ``.env``.

    Raises :class:`MissingCredentials` naming the unset variable, rather than letting
    the SDK fail later with an authentication error that says nothing about which half
    of the credential is missing.
    """
    source = dict(load_env_file())
    source.update(env if env is not None else os.environ)

    key_id = (source.get(KEY_ID_VAR) or "").strip()
    key_secret = (source.get(KEY_SECRET_VAR) or "").strip()
    missing = [n for n, v in ((KEY_ID_VAR, key_id), (KEY_SECRET_VAR, key_secret)) if not v]
    if missing:
        raise MissingCredentials(
            f"{' and '.join(missing)} not set. Razorpay test-mode keys go in .env "
            f"(see .env.example); .gitignore excludes it. This transport is optional -- "
            f"the published numbers come from the mock."
        )
    if not key_id.startswith(TEST_KEY_PREFIX):
        raise LiveKeyRefused(
            f"{KEY_ID_VAR} is {key_id[:12]!r}..., which is not a {TEST_KEY_PREFIX!r} key. "
            f"This project never runs against a live Razorpay account."
        )
    return key_id, key_secret


def has_credentials() -> bool:
    """Whether a live test-mode run is possible. Used to skip the live test marker."""
    try:
        credentials_from_env()
    except (MissingCredentials, LiveKeyRefused):
        return False
    return True


def build_client(env: dict[str, str] | None = None):
    """A ``razorpay.Client`` on test-mode credentials.

    The SDK is imported here rather than at module scope so that importing this module
    -- which ``tests/test_adversarial.py`` does, to skip on -- never depends on the
    package being installed.
    """
    import razorpay  # noqa: PLC0415  (deliberate: keeps the import optional)

    key_id, key_secret = credentials_from_env(env)
    client = razorpay.Client(auth=(key_id, key_secret))
    client.set_app_details({"title": "razorpay-track03-recovery", "version": "0.1.0"})
    return client


# -- per-case live state -------------------------------------------------------------


@dataclass
class _LiveCase:
    """What this transport knows about one case.

    Note what is not here: no latents, no success probability, no schedule. There is no
    outcome model on this side of the boundary -- whether a payment succeeds is decided
    by Razorpay and arrives asynchronously over a webhook, which is exactly the reason
    a live run cannot produce a reproducible headline.
    """

    case: Case
    status: str = STATUS_OPEN
    attempts: list[Attempt] = field(default_factory=list)
    contacts: list[Contact] = field(default_factory=list)
    recovered_at_hour: int | None = None
    closed_reason: str | None = None
    cost_paise: int = 0
    order_ids: list[str] = field(default_factory=list)
    payment_link_ids: list[str] = field(default_factory=list)
    razorpay_payment_id: str | None = None

    @property
    def has_recovered(self) -> bool:
        return self.recovered_at_hour is not None

    @property
    def accepts_actions(self) -> bool:
        return not self.has_recovered and self.closed_reason is None


class RazorpayTestTransport:
    """Test-mode Razorpay behind the :class:`Transport` protocol.

    One instance per arm, matching ``MockTransport``. Construct with
    :meth:`from_env`; the explicit constructor takes an already-built client so a test
    can pass a stub without any credential being present.
    """

    def __init__(
        self,
        cases: Sequence[Case],
        client: Any,
        params: SimParams | None = None,
        *,
        notes: dict[str, str] | None = None,
    ) -> None:
        self.client = client
        self.params = params or SimParams()
        self.notes = dict(notes or {})
        # Sorted for the same reason the mock sorts: dict iteration order deciding the
        # order of outbound calls is a latent non-determinism bug even when nothing
        # random is being drawn.
        self._cases: dict[str, _LiveCase] = {
            c.case_id: _LiveCase(case=c) for c in sorted(cases, key=lambda c: c.case_id)
        }
        self._hour = -1

    @classmethod
    def from_env(
        cls,
        cases: Sequence[Case],
        params: SimParams | None = None,
        *,
        env: dict[str, str] | None = None,
    ) -> "RazorpayTestTransport":
        return cls(cases, client=build_client(env), params=params)

    # -- clock -----------------------------------------------------------------------

    def tick(self, at_hour: int) -> None:
        """A no-op with a real clock behind it.

        The mock resolves scheduled events here. Live, nothing is scheduled locally:
        a payment succeeds when a customer pays, and the system learns about it through
        :mod:`recovery.transport.webhooks`. Recording the hour keeps ``observe`` honest
        about what it can see.
        """
        self._hour = at_hour

    # -- the debit path ---------------------------------------------------------------

    def attempt_retry(self, case_id: str, at_hour: int, idempotency_key: str) -> Result:
        """Re-present the payment. E-mandate goes down the recurring-charge path.

        **A live retry never returns ``recovered=True`` synchronously**, and that is
        correct rather than a limitation: creating an order does not collect money, and
        even a recurring charge is authorised asynchronously. Recovery is reported by
        the webhook receiver. Any arm that treated a successful API call as a recovery
        would be counting intents as payments.
        """
        live = self._case(case_id)
        if not live.accepts_actions:
            return Result(
                ok=False,
                kind="retry",
                recovered=False,
                cost_paise=0,
                detail={"rejected": "terminal", "status": live.status},
            )

        attempt_no = len(live.attempts) + 1
        cost = self.params.costs.retry_paise
        live.cost_paise += cost

        try:
            if live.case.method is Method.EMANDATE:
                detail = self._charge_mandate(live, idempotency_key)
            else:
                detail = self._create_order(live, idempotency_key)
            ok = True
        except Exception as exc:  # noqa: BLE001 -- the SDK raises several error types
            ok = False
            detail = {"error": type(exc).__name__, "message": str(exc)[:400]}

        live.attempts.append(Attempt(hour=at_hour, attempt_no=attempt_no, succeeded=False))
        detail.update({"attempt_no": attempt_no, "at_hour": at_hour, "succeeded": False})
        return Result(ok=ok, kind="retry", recovered=False, cost_paise=cost, detail=detail)

    def _create_order(self, live: _LiveCase, idempotency_key: str) -> dict:
        """The card/UPI retry path.

        ``receipt`` carries the idempotency key. Razorpay treats the receipt as the
        merchant's own reference, so a retry-after-timeout that reuses the key is
        traceable to a single intent from the merchant's side of the ledger -- which is
        what makes the ``Idempotency`` rule enforceable against a real account rather
        than only against the simulator.
        """
        order = self.client.order.create(
            {
                "amount": live.case.amount_paise,  # paise, no conversion
                "currency": CURRENCY,
                "receipt": idempotency_key,
                "notes": {
                    "case_id": live.case.case_id,
                    "decline_reason": live.case.decline_reason,
                    **self.notes,
                },
            }
        )
        live.order_ids.append(order["id"])
        return {"path": "order", "order_id": order["id"], "status": order.get("status")}

    def _charge_mandate(self, live: _LiveCase, idempotency_key: str) -> dict:
        """"Charge this now" against an existing e-mandate -- the debit the RBI rules govern.

        Requires a saved token from an authorised mandate, carried on the case's notes.
        Without one the call is refused locally with a message naming what is missing,
        rather than sent to Razorpay to fail with a generic bad-request.
        """
        token = self.notes.get("token_id")
        customer_id = self.notes.get("razorpay_customer_id")
        if not token or not customer_id:
            raise ValueError(
                "an e-mandate debit needs an authorised token_id and razorpay_customer_id; "
                "neither is configured, so there is no mandate to charge"
            )
        order = self.client.order.create(
            {
                "amount": live.case.amount_paise,
                "currency": CURRENCY,
                "receipt": idempotency_key,
                "payment_capture": True,
                "notes": {"case_id": live.case.case_id, **self.notes},
            }
        )
        live.order_ids.append(order["id"])
        payment = self.client.payment.createRecurring(
            {
                "email": self.notes.get("email", ""),
                "contact": self.notes.get("contact", ""),
                "amount": live.case.amount_paise,
                "currency": CURRENCY,
                "order_id": order["id"],
                "customer_id": customer_id,
                "token": token,
                "recurring": "1",
                "description": f"recovery debit for {live.case.case_id}",
            }
        )
        live.razorpay_payment_id = payment.get("razorpay_payment_id") or payment.get("id")
        return {
            "path": "recurring",
            "order_id": order["id"],
            "payment_id": live.razorpay_payment_id,
        }

    # -- the contact path --------------------------------------------------------------

    def send_message(
        self,
        case_id: str,
        channel: str,
        template: str,
        at_hour: int,
        idempotency_key: str,
    ) -> Result:
        """Create a Payment Link and have Razorpay notify the customer.

        The link *is* the message: a dunning nudge that cannot be paid from is a nudge
        that measures nothing. ``reference_id`` carries the idempotency key, so a
        duplicated send is rejected by Razorpay itself rather than only by the policy
        engine -- two independent enforcement points for the same invariant.
        """
        live = self._case(case_id)
        if not live.accepts_actions:
            return Result(
                ok=False,
                kind="message",
                recovered=False,
                cost_paise=0,
                detail={"rejected": "terminal", "status": live.status},
            )
        if channel == CHANNEL_WHATSAPP:
            return Result(
                ok=False,
                kind="message",
                recovered=False,
                cost_paise=0,
                detail={
                    "error": "unsupported_channel",
                    "message": (
                        "WhatsApp is a separate Razorpay product with its own onboarding; "
                        "this transport will not silently downgrade the channel to SMS"
                    ),
                    "channel": channel,
                },
            )

        cost = self.params.costs.message_paise(channel)
        live.cost_paise += cost
        expire_by = live.case.failed_at + timedelta(hours=at_hour + PAYMENT_LINK_EXPIRY_H)

        try:
            link = self.client.payment_link.create(
                {
                    "amount": live.case.amount_paise,
                    "currency": CURRENCY,
                    "accept_partial": False,
                    "reference_id": idempotency_key,
                    "description": f"{template} for {live.case.case_id}",
                    "expire_by": int(expire_by.timestamp()),
                    "reminder_enable": False,  # our policy engine owns the cadence
                    "notify": {
                        "sms": channel == CHANNEL_SMS,
                        "email": channel == CHANNEL_EMAIL,
                    },
                    "notes": {"case_id": live.case.case_id, "template": template, **self.notes},
                }
            )
            live.payment_link_ids.append(link["id"])
            detail = {
                "payment_link_id": link["id"],
                "short_url": link.get("short_url"),
                "status": link.get("status"),
            }
            ok = True
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = {"error": type(exc).__name__, "message": str(exc)[:400]}

        live.contacts.append(Contact(hour=at_hour, channel=channel, template=template))
        detail.update({"channel": channel, "template": template, "at_hour": at_hour})
        return Result(ok=ok, kind="message", recovered=False, cost_paise=cost, detail=detail)

    # -- terminal ---------------------------------------------------------------------

    def close_case(self, case_id: str, at_hour: int, reason: str) -> Result:
        """Stop working the case, and cancel any link that could still collect.

        The mock leaves a closed case able to self-heal, because closing is an internal
        decision that does not reach the customer. Live, an outstanding payment link
        *is* reachable, so leaving it payable would let a case we have written off
        collect money weeks later and land as an unattributable recovery. Cancelling is
        the honest live analogue of the mock's semantics.
        """
        live = self._case(case_id)
        if not live.accepts_actions:
            return Result(
                ok=False,
                kind="close",
                recovered=False,
                cost_paise=0,
                detail={"rejected": "terminal", "status": live.status},
            )

        cancelled, failures = [], []
        for link_id in live.payment_link_ids:
            try:
                self.client.payment_link.cancel(link_id)
                cancelled.append(link_id)
            except Exception as exc:  # noqa: BLE001 -- an already-paid link cannot cancel
                failures.append({"payment_link_id": link_id, "error": str(exc)[:200]})

        live.status = STATUS_CLOSED
        live.closed_reason = reason
        return Result(
            ok=True,
            kind="close",
            recovered=False,
            cost_paise=0,
            detail={"reason": reason, "links_cancelled": cancelled, "cancel_failures": failures},
        )

    # -- inbound ----------------------------------------------------------------------

    def mark_recovered(self, case_id: str, at_hour: int, payment_id: str | None = None) -> bool:
        """Record a confirmed payment. Returns whether this call changed anything.

        The only way a live case becomes ``recovered``. Called by
        :class:`~recovery.transport.webhooks.WebhookReceiver` and by nothing else --
        which is what keeps at-least-once delivery safe: the idempotence lives here, in
        one place, rather than being re-derived by each caller.
        """
        live = self._case(case_id)
        if live.has_recovered:
            return False
        live.status = STATUS_RECOVERED
        live.recovered_at_hour = at_hour
        live.razorpay_payment_id = payment_id or live.razorpay_payment_id
        return True

    # -- observation -------------------------------------------------------------------

    def observe(self, case_id: str, at_hour: int) -> CaseState:
        """State as of ``at_hour``, from local records only.

        Deliberately not a network call. ``observe`` runs once per open case per
        simulated hour; fetching each one would be 168,000 requests for a 500-case
        window and would make the arm's view depend on API latency. Status changes
        arrive by webhook instead, which is how a production system learns them too.
        """
        live = self._case(case_id)
        case = live.case

        recovered_at = live.recovered_at_hour
        if recovered_at is not None and recovered_at <= at_hour:
            status, visible_recovered_at = STATUS_RECOVERED, recovered_at
        elif live.closed_reason is not None:
            status, visible_recovered_at = STATUS_CLOSED, None
        else:
            status, visible_recovered_at = STATUS_OPEN, None

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
            attempts=tuple(a for a in live.attempts if a.hour <= at_hour),
            contacts=tuple(c for c in live.contacts if c.hour <= at_hour),
            recovered_at_hour=visible_recovered_at,
        )

    def case_ids(self) -> tuple[str, ...]:
        return tuple(self._cases)

    def _case(self, case_id: str) -> _LiveCase:
        try:
            return self._cases[case_id]
        except KeyError:
            raise KeyError(f"no such case in this cohort: {case_id!r}") from None
