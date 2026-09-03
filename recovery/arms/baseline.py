"""Razorpay's T+3 default: retry once a day for three days. That is the whole policy.

No contact, no reason-awareness, no notices, no AFA. It is the floor the agent has to
beat, and having it on the board means the submission has a real measured result even
if the agent arm never lands.

It routes through the policy gate like every other arm, and the consequence is worth
watching rather than working around: the gate **denies** its retries on HARD and ACTION
declines, on e-mandate debits with no pre-debit notice, and on first debits without AFA.
That is exactly the naive behaviour the rules exist to prevent, and those denials
populate the blocked-action log with real entries -- the naive arm is what proves the
gate does something.
"""

from __future__ import annotations

from recovery.arms.control import ArmContext
from recovery.transport.base import ACTION_RETRY, Action, CaseState, Transport

RETRY_HOURS = (24, 48, 72)
"""T+1, T+2, T+3. Once a day for three days, regardless of what actually failed."""


class BaselineArm:
    name = "baseline"

    def __init__(self, ctx: ArmContext) -> None:
        if ctx.gate is None:
            raise ValueError("the baseline arm acts, so it must be given a policy gate")
        self.ctx = ctx
        self.gate = ctx.gate

    def act(self, state: CaseState, hour: int, transport: Transport) -> list[Action]:
        if hour not in RETRY_HOURS:
            return []

        action = Action(
            type=ACTION_RETRY,
            case_id=state.case_id,
            at_hour=hour,
            # Deterministic and stable, so a rerun reuses the same key and the
            # idempotency guard is exercised rather than sidestepped.
            idempotency_key=f"{state.case_id}:retry:t+{hour // 24}",
        )
        decision, result = self.gate.execute(action, state)
        return [action] if result is not None else []
