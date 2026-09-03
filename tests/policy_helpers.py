"""Helpers for testing one policy rule at a time.

A rule is a pure function of ``(action, PolicyContext)``, so these build a context
directly rather than replaying a run to arrive at one. Tests that need the whole
engine, the ledger writes and the transport gating go through ``make_gate`` in
``conftest`` instead.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from recovery.policy.engine import LedgerIndex, PolicyContext, Verdict
from recovery.scoring import Scorer
from recovery.transport.base import Action, CaseState, SimParams


def context_for(
    state: CaseState,
    *,
    at_hour: int | None = None,
    params: SimParams | None = None,
    used_keys: Sequence[str] = (),
    case_spend_paise: int = 0,
    contact_hours: Sequence[int] = (),
    notices: Sequence[tuple[datetime, dict]] = (),
    scorer: Scorer | None = None,
) -> PolicyContext:
    """A policy context with hand-set history.

    ``notices`` are ``(sent_at, metadata)`` pairs, so the RBI pre-debit boundary can be
    probed at minute resolution rather than at the simulator's hourly granularity.
    """
    params = params or SimParams()
    index = LedgerIndex()
    for key in used_keys:
        index.used_keys.add(key)
    if case_spend_paise:
        index.case_spend[state.case_id] = case_spend_paise
    for hour in contact_hours:
        index.customer_contact_hours.setdefault(state.customer_id, []).append(hour)
    for sent_at, metadata in notices:
        index.record_notice(
            case_id=state.case_id,
            template="pre_debit_notice",
            sent_at=sent_at,
            metadata=metadata,
        )

    hour = state.hour if at_hour is None else at_hour
    return PolicyContext(
        state=state,
        clock=state.failed_at + timedelta(hours=hour),
        at_hour=hour,
        history=index,
        scorer=scorer or Scorer(params=params),
        params=params,
    )


def verdict_for(rule, action: Action, state: CaseState, **context_kwargs) -> Verdict:
    """Run a single rule against a freshly built context."""
    ctx = context_for(state, at_hour=action.at_hour, **context_kwargs)
    return rule.check(action, ctx)
