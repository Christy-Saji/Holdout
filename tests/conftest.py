"""Shared fixtures: hand-built cases with chosen latents.

Cohort-generated cases have latents drawn from the generator, which is right for
end-to-end tests and wrong for testing one mechanism at a time. These builders let a
test say "a stolen-card case whose customer always pays" and then assert that the
retry still never succeeds.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Sequence

from recovery.cohort import IST, Case, CustomerProfile, Latents
from recovery.declines import Method, lookup
from recovery.transport.base import STATUS_OPEN, Attempt, CaseState, Contact, SimParams

FIXED_FAILED_AT = datetime(2026, 8, 12, 10, 0, tzinfo=IST)
"""A Wednesday mid-morning, mid-month: outside quiet hours and away from the payroll
boundary, so neither modifier fires unless a test asks for it."""


def make_latents(
    *,
    p_self_heal: float = 0.0,
    retry_success_base: float = 0.5,
    intent_to_pay: float = 1.0,
    responsiveness: float = 1.0,
    affinity: float = 1.0,
) -> Latents:
    return Latents(
        p_self_heal=p_self_heal,
        retry_success_base=retry_success_base,
        intent_to_pay=intent_to_pay,
        responsiveness=responsiveness,
        channel_affinity={"sms": affinity, "whatsapp": affinity, "email": affinity},
    )


def make_case(
    case_id: str = "case-00000",
    *,
    decline_reason: str = "insufficient_funds",
    method: Method | None = None,
    amount_paise: int = 50_000,
    customer_id: str | None = None,
    segment: str = "regular",
    failed_at: datetime = FIXED_FAILED_AT,
    is_recurring: bool = True,
    mandate_first_debit: bool = False,
    mandate_category: str = "standard",
    latents: Latents | None = None,
    **latent_kwargs,
) -> Case:
    """A case with hand-chosen latents.

    ``retry_success_base`` defaults to the taxonomy's value for the reason, so a HARD
    or ACTION case is zero by construction exactly as it would be in a real cohort.
    """
    reason = lookup(decline_reason)
    if method is None:
        method = sorted(reason.methods, key=lambda m: m.value)[0]
    if latents is None:
        latent_kwargs.setdefault("retry_success_base", reason.retry_success_base)
        latents = make_latents(**latent_kwargs)

    return Case(
        case_id=case_id,
        customer=CustomerProfile(customer_id=customer_id or f"cust-{case_id[-5:]}", segment=segment),
        payment_id=f"pay_{case_id}",
        amount_paise=amount_paise,
        method=method,
        decline_reason=decline_reason,
        failed_at=failed_at,
        is_recurring=is_recurring,
        mandate_first_debit=mandate_first_debit,
        mandate_category=mandate_category,
        latents=latents,
    )


def make_state(
    case: Case | None = None,
    *,
    hour: int = 0,
    status: str = STATUS_OPEN,
    attempts: Sequence[Attempt] = (),
    contacts: Sequence[Contact] = (),
    recovered_at_hour: int | None = None,
    **case_kwargs,
) -> CaseState:
    """The arm-visible view of a case, built directly.

    Policy rules are pure functions of this plus the ledger, so building it by hand is
    the honest way to test one rule at a time.
    """
    case = case or make_case(**case_kwargs)
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
        hour=hour,
        status=status,
        attempts=tuple(attempts),
        contacts=tuple(contacts),
        recovered_at_hour=recovered_at_hour,
    )


def make_gate(
    tmp_path: Path,
    cases: Sequence[Case],
    *,
    arm: str = "test",
    params: SimParams | None = None,
    seed: int = 42,
):
    """A real gate over a real ledger and a real simulator.

    Every policy test routes through this rather than calling a rule in isolation, so
    the "no money or contact action bypasses the engine" invariant is exercised by the
    tests themselves rather than merely asserted about the production path.
    """
    from recovery.ledger import Ledger
    from recovery.policy.engine import Gate, default_engine
    from recovery.scoring import Scorer
    from recovery.transport.mock import MockTransport

    params = params or SimParams()
    transport = MockTransport(cases, seed=seed, params=params)
    ledger = Ledger(Path(tmp_path) / f"{arm}.jsonl", run_id="run-test")
    scorer = Scorer(params=params)
    return Gate(
        engine=default_engine(scorer),
        ledger=ledger,
        transport=transport,
        arm=arm,
        params=params,
    )
