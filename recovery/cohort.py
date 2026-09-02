"""Synthetic cohort of failed recurring payments, with hidden latent ground truth.

Two things in this module are load-bearing for the whole measurement design.

**Common random numbers.** Every stochastic draw in this project goes through ``u()``,
which is keyed by the *identity* of the event -- ``(seed, case_id, event_kind,
sequence)`` -- rather than by position in an RNG stream. The consequence is that the
hour at which case #237 would self-heal is identical in the control arm and the agent
arm, so the difference between arms is attributable purely to the intervention rather
than to sampling noise. That tightens the confidence interval on incremental lift for
the same cohort size, and it means arms can run in any order, in parallel, or in
separate processes with the comparison staying valid. Never ``random.random()``, never
a sequential ``numpy.Generator`` call whose value depends on how many draws preceded
it.

**Latents are hidden.** ``Latents`` is drawn at generation time and read *only* by the
simulator (phase 3). No arm, no scorer and no policy rule may read it. Because the
simulator owns the latents the counterfactual is exact rather than estimated -- we are
not inferring what would have happened, we specified it. This is enforced
structurally, not by convention: :func:`write_cohort` writes the arm-facing cohort
with **no latents in it at all**, and the latents go to a separate side-table keyed by
``case_id`` that only the transport layer loads. A case loaded without that side table
has ``latents is None``, so a rule that peeks gets an ``AttributeError`` rather than an
answer.

All money is integer paise. Never float rupees.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from recovery.declines import Method, lookup, reasons_for_method

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
"""Every simulated timestamp in this project is IST."""

DEFAULT_SEED = 42
DEFAULT_N = 500
COHORT_PATH = Path("data/cohort_seed42.jsonl")

CHANNELS = ("sms", "whatsapp", "email")
SEGMENTS = ("new", "regular", "loyal")
MANDATE_CATEGORIES = ("standard", "insurance", "mutual_fund", "credit_card_bill")

# The cohort's clock origin. Fixed, so that regenerating the artifact on a different
# day cannot change a single byte of it.
COHORT_EPOCH = datetime(2026, 8, 1, 0, 0, tzinfo=IST)
COHORT_SPAN_H = 30 * 24


# --------------------------------------------------------------------------------
# Common random numbers
# --------------------------------------------------------------------------------


def u(seed: int, case_id: str, event_kind: str, sequence: int) -> float:
    """Uniform [0,1) draw keyed by event identity, not by stream position."""
    h = hashlib.blake2b(f"{seed}|{case_id}|{event_kind}|{sequence}".encode())
    return int.from_bytes(h.digest()[:8], "big") / 2**64


def _pick(weights: list[float], x: float) -> int:
    """Inverse-transform sample: index into ``weights`` (which must sum to 1) for the
    uniform draw ``x``. Pure function of ``x`` -- no hidden stream state."""
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if x < acc:
            return i
    return len(weights) - 1  # only reachable through float slop at x -> 1


def _normal(seed: int, case_id: str, event_kind: str) -> float:
    """Standard normal via Box-Muller from two CRN uniforms."""
    u1 = max(u(seed, case_id, event_kind, 0), 1e-12)  # log(0) guard
    u2 = u(seed, case_id, event_kind, 1)
    return math.sqrt(-2.0 * math.log(u1)) * math.cos(2.0 * math.pi * u2)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


# --------------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Latents:
    """Hidden ground truth. Read by the simulator only -- never by an arm, the scorer
    or the policy engine."""

    p_self_heal: float
    """P(recovers inside the 336h window with no intervention)."""

    retry_success_base: float
    """P(a well-timed first retry succeeds)."""

    intent_to_pay: float
    """Per-customer willingness multiplier."""

    responsiveness: float
    """Per-customer contact-effectiveness multiplier."""

    channel_affinity: dict
    """Per-channel multiplier: sms / whatsapp / email."""


@dataclass(frozen=True)
class CustomerProfile:
    customer_id: str
    segment: str  # new | regular | loyal


@dataclass(frozen=True)
class Case:
    case_id: str
    customer: CustomerProfile
    payment_id: str
    amount_paise: int  # integer paise everywhere; never float rupees
    method: Method  # card | upi | netbanking | emandate | wallet
    decline_reason: str
    failed_at: datetime  # IST
    is_recurring: bool
    mandate_first_debit: bool  # AFA required regardless of amount (phase 4)
    mandate_category: str  # standard | insurance | mutual_fund | credit_card_bill

    latents: Latents | None
    """HIDDEN from every arm. ``None`` whenever the case was loaded without the
    latents side table, which is how every arm loads it."""


# --------------------------------------------------------------------------------
# Generation priors -- all of these are our own, and all are swept in phase 6
# --------------------------------------------------------------------------------

# UPI-heavy, matching Indian payment reality.
_METHOD_MIX: tuple[tuple[Method, float], ...] = (
    (Method.UPI, 0.55),
    (Method.CARD, 0.25),
    (Method.EMANDATE, 0.09),
    (Method.NETBANKING, 0.08),
    (Method.WALLET, 0.03),
)

_SEGMENT_MIX: tuple[tuple[str, float], ...] = (
    ("new", 0.30),
    ("regular", 0.50),
    ("loyal", 0.20),
)

# segment -> (base intent_to_pay, base responsiveness)
_SEGMENT_TRAITS: dict[str, tuple[float, float]] = {
    "new": (0.60, 0.50),
    "regular": (0.80, 0.70),
    "loyal": (0.95, 0.85),
}

_MANDATE_CATEGORY_MIX: tuple[tuple[str, float], ...] = (
    ("standard", 0.70),
    ("insurance", 0.10),
    ("mutual_fund", 0.10),
    ("credit_card_bill", 0.10),
)

# Subscription price points, in whole rupees. A real recurring cohort clusters hard on
# a handful of published plan prices rather than spreading smoothly.
_SUBSCRIPTION_RUPEES: tuple[int, ...] = (99, 149, 199, 299, 399, 499, 649, 999, 1499)
_P_SUBSCRIPTION = 0.35

# Log-normal for the non-subscription tail: median ~Rs.800, mass in Rs.200-5,000.
_AMOUNT_LN_MU = math.log(800.0)
_AMOUNT_LN_SIGMA = 0.90
_AMOUNT_MIN_RUPEES = 50.0
_AMOUNT_MAX_RUPEES = 200_000.0

_P_RECURRING = 0.88
_P_FIRST_DEBIT = 0.15
_CUSTOMER_POOL_FRACTION = 0.70  # <1 so some customers carry several failed payments


def _customer_pool_size(n: int) -> int:
    return max(1, round(n * _CUSTOMER_POOL_FRACTION))


def _make_customer(seed: int, customer_id: str) -> tuple[CustomerProfile, dict]:
    """Build a customer and its per-customer latent traits.

    Keyed on ``customer_id`` rather than ``case_id`` so that two failed payments from
    the same customer share one personality, which is what makes contact fatigue mean
    anything in phase 4.
    """
    seg_names = [s for s, _ in _SEGMENT_MIX]
    seg_weights = [w for _, w in _SEGMENT_MIX]
    segment = seg_names[_pick(seg_weights, u(seed, customer_id, "segment", 0))]
    intent_base, resp_base = _SEGMENT_TRAITS[segment]

    intent = _clamp(intent_base * (0.85 + 0.30 * u(seed, customer_id, "intent", 0)), 0.05, 1.0)
    resp = _clamp(resp_base * (0.85 + 0.30 * u(seed, customer_id, "responsiveness", 0)), 0.05, 1.0)
    affinity = {
        c: round(0.60 + 0.40 * u(seed, customer_id, f"affinity_{c}", 0), 6) for c in CHANNELS
    }

    return (
        CustomerProfile(customer_id=customer_id, segment=segment),
        {"intent_to_pay": round(intent, 6), "responsiveness": round(resp, 6), "channel_affinity": affinity},
    )


def _draw_amount_paise(seed: int, case_id: str) -> int:
    """Integer paise. Rupees are rounded *before* the conversion -- ``int(x * 100)``
    is the classic money bug (``int(2.675 * 100) == 267``)."""
    if u(seed, case_id, "amount_cluster", 0) < _P_SUBSCRIPTION:
        idx = _pick(
            [1.0 / len(_SUBSCRIPTION_RUPEES)] * len(_SUBSCRIPTION_RUPEES),
            u(seed, case_id, "amount_plan", 0),
        )
        return _SUBSCRIPTION_RUPEES[idx] * 100

    rupees = math.exp(_AMOUNT_LN_MU + _AMOUNT_LN_SIGMA * _normal(seed, case_id, "amount_ln"))
    rupees = _clamp(rupees, _AMOUNT_MIN_RUPEES, _AMOUNT_MAX_RUPEES)
    return int(round(rupees)) * 100


def _draw_method(seed: int, case_id: str) -> Method:
    names = [m for m, _ in _METHOD_MIX]
    weights = [w for _, w in _METHOD_MIX]
    return names[_pick(weights, u(seed, case_id, "method", 0))]


def _draw_reason(seed: int, case_id: str, method: Method) -> str:
    """Sample by population weight, restricted to reasons valid for ``method`` and
    renormalised over that subset.

    Note the honest consequence: a reason's *realised* share of the cohort is not its
    declared ``population_weight``. Conditioning on a UPI-heavy method mix removes the
    card-only reasons from most draws, so ``insufficient_funds`` -- valid on every
    method, declared at 0.28 -- lands near 0.41 of the seed-42 cohort. Coincidentally
    close to the 44% card-not-present anchor, but arrived at sideways; the declared
    weights are the marginal prior, not the realised frequency.
    """
    candidates = reasons_for_method(method)
    total = sum(r.population_weight for r in candidates)
    weights = [r.population_weight / total for r in candidates]
    return candidates[_pick(weights, u(seed, case_id, "decline_reason", 0))].code


def _draw_latents(seed: int, case_id: str, reason_code: str, traits: dict) -> Latents:
    reason = lookup(reason_code)
    # A willing customer is likelier to notice and fix the failure unprompted. HARD
    # reasons have self_heal_rate 0.0, so this stays 0.0 for them by construction.
    p_self_heal = _clamp(reason.self_heal_rate * (0.60 + 0.80 * traits["intent_to_pay"]), 0.0, 0.95)
    return Latents(
        p_self_heal=round(p_self_heal, 6),
        retry_success_base=reason.retry_success_base,
        intent_to_pay=traits["intent_to_pay"],
        responsiveness=traits["responsiveness"],
        channel_affinity=dict(traits["channel_affinity"]),
    )


def generate_cohort(seed: int = DEFAULT_SEED, n: int = DEFAULT_N) -> list[Case]:
    """Generate ``n`` failed recurring payments. Fully determined by ``seed``."""
    if n < 1:
        raise ValueError(f"cohort size must be >= 1, got {n}")

    pool = _customer_pool_size(n)
    cat_names = [c for c, _ in _MANDATE_CATEGORY_MIX]
    cat_weights = [w for _, w in _MANDATE_CATEGORY_MIX]

    cases: list[Case] = []
    for i in range(n):
        case_id = f"case-{i:05d}"

        cust_idx = min(int(u(seed, case_id, "customer", 0) * pool), pool - 1)
        customer, traits = _make_customer(seed, f"cust-{cust_idx:05d}")

        method = _draw_method(seed, case_id)
        reason_code = _draw_reason(seed, case_id, method)
        failed_at = COHORT_EPOCH + timedelta(
            hours=int(u(seed, case_id, "failed_at", 0) * COHORT_SPAN_H)
        )

        cases.append(
            Case(
                case_id=case_id,
                customer=customer,
                payment_id=f"pay_{seed}{i:06d}",
                amount_paise=_draw_amount_paise(seed, case_id),
                method=method,
                decline_reason=reason_code,
                failed_at=failed_at,
                is_recurring=u(seed, case_id, "is_recurring", 0) < _P_RECURRING,
                mandate_first_debit=u(seed, case_id, "first_debit", 0) < _P_FIRST_DEBIT,
                mandate_category=cat_names[
                    _pick(cat_weights, u(seed, case_id, "mandate_category", 0))
                ],
                latents=_draw_latents(seed, case_id, reason_code, traits),
            )
        )

    return cases


# --------------------------------------------------------------------------------
# Serialisation -- the public cohort and the latents side table are separate files
# --------------------------------------------------------------------------------


def case_to_public_dict(case: Case) -> dict:
    """The arm-facing view of a case. Contains no latents, by construction."""
    return {
        "case_id": case.case_id,
        "customer_id": case.customer.customer_id,
        "segment": case.customer.segment,
        "payment_id": case.payment_id,
        "amount_paise": case.amount_paise,
        "method": case.method.value,
        "decline_reason": case.decline_reason,
        "failed_at": case.failed_at.isoformat(),
        "is_recurring": case.is_recurring,
        "mandate_first_debit": case.mandate_first_debit,
        "mandate_category": case.mandate_category,
    }


def latents_to_dict(case: Case) -> dict:
    """The side-table row for a case. Loaded by the transport layer only."""
    assert case.latents is not None, f"{case.case_id} has no latents to serialise"
    return {
        "case_id": case.case_id,
        "p_self_heal": case.latents.p_self_heal,
        "retry_success_base": case.latents.retry_success_base,
        "intent_to_pay": case.latents.intent_to_pay,
        "responsiveness": case.latents.responsiveness,
        "channel_affinity": case.latents.channel_affinity,
    }


def _dumps(obj: dict) -> str:
    """Canonical one-line JSON: sorted keys, no incidental whitespace. Byte-stability
    is what makes the reproduction gate in phase 8 meaningful."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def serialise_cohort(cases: list[Case]) -> str:
    """The public cohort as JSONL text. Used by the byte-identity test."""
    return "".join(_dumps(case_to_public_dict(c)) + "\n" for c in cases)


def serialise_latents(cases: list[Case]) -> str:
    return "".join(_dumps(latents_to_dict(c)) + "\n" for c in cases)


def latents_path_for(path: Path | str) -> Path:
    """``data/cohort_seed42.jsonl`` -> ``data/cohort_seed42.latents.jsonl``."""
    p = Path(path)
    return p.with_suffix(".latents" + p.suffix)


def write_cohort(cases: list[Case], path: Path | str = COHORT_PATH) -> Path:
    """Write the public cohort and its latents side table. Returns the cohort path."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(serialise_cohort(cases), encoding="utf-8", newline="\n")
    latents_path_for(p).write_text(serialise_latents(cases), encoding="utf-8", newline="\n")
    return p


def load_cohort(path: Path | str = COHORT_PATH, *, with_latents: bool = False) -> list[Case]:
    """Load the cohort.

    ``with_latents`` is for the simulator and nothing else. Every arm, the scorer and
    the policy engine load with the default, and get ``latents is None``.
    """
    p = Path(path)
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]

    side: dict[str, Latents] = {}
    if with_latents:
        lp = latents_path_for(p)
        for line in lp.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            d = json.loads(line)
            side[d["case_id"]] = Latents(
                p_self_heal=d["p_self_heal"],
                retry_success_base=d["retry_success_base"],
                intent_to_pay=d["intent_to_pay"],
                responsiveness=d["responsiveness"],
                channel_affinity=d["channel_affinity"],
            )

    return [
        Case(
            case_id=r["case_id"],
            customer=CustomerProfile(customer_id=r["customer_id"], segment=r["segment"]),
            payment_id=r["payment_id"],
            amount_paise=r["amount_paise"],
            method=Method(r["method"]),
            decline_reason=r["decline_reason"],
            failed_at=datetime.fromisoformat(r["failed_at"]),
            is_recurring=r["is_recurring"],
            mandate_first_debit=r["mandate_first_debit"],
            mandate_category=r["mandate_category"],
            latents=side.get(r["case_id"]) if with_latents else None,
        )
        for r in rows
    ]


def cohort_path_for(seed: int) -> Path:
    return Path("data") / f"cohort_seed{seed}.jsonl"


def build_cohort_artifact(seed: int = DEFAULT_SEED, n: int = DEFAULT_N, out: Path | str | None = None) -> Path:
    """Generate and write the committed cohort artifact. Idempotent for a given seed."""
    cases = generate_cohort(seed=seed, n=n)
    return write_cohort(cases, out or cohort_path_for(seed))
