"""The policy-gated agent arm.

**One planning call per case, at hour 0.** ``act()`` is called every hour of a 336-hour
window, so a model call per hour would be 168,000 requests for a 500-case run. Instead
the agent plans the whole case once -- a multi-turn tool loop that inspects the case,
dry-runs candidate actions against the policy engine and commits a schedule -- and this
arm then fires each scheduled action through the gate at the hour it comes due.

The honest cost of that choice: **the agent does not re-plan when a retry fails.** A
production system would, and a version of this that did would recover more. It is a
deliberate trade of agent quality for a run that costs tens of rupees instead of
thousands, and it belongs in the write-up as a limitation rather than being quietly
omitted.

Two properties hold regardless of what the model decides:

- Every action it schedules was screened by ``PolicyEngine.evaluate()`` at plan time
  and is gated **again** by ``Gate.execute()`` at the hour it fires, against the state
  as it then is. A plan is a proposal, never an authorisation.
- The agent never sees latents. It observes only through ``CaseState``, which has no
  field to read them from.
"""

from __future__ import annotations

from pathlib import Path

from recovery.agent.runner import (
    DEFAULT_CASSETTE_ROOT,
    MODE_REPLAY,
    CassetteStore,
    PlanResult,
    Planner,
)
from recovery.arms.control import ArmContext
from recovery.transport.base import Action, CaseState, Transport


class AgentArm:
    """Plans each case once with Claude, then executes the plan through the gate."""

    name = "agent"

    def __init__(self, ctx: ArmContext) -> None:
        if ctx.gate is None:
            raise ValueError("the agent arm acts, so it must be given a policy gate")
        self.ctx = ctx
        self.gate = ctx.gate
        self.store = CassetteStore(
            root=Path(ctx.options.get("cassettes") or DEFAULT_CASSETTE_ROOT),
            mode=str(ctx.options.get("mode") or MODE_REPLAY),
        )
        self.planner = ctx.options.get("planner") or Planner(
            gate=self.gate, params=ctx.params, store=self.store
        )
        self.plans: dict[str, PlanResult] = {}
        self._queue: dict[str, list[Action]] = {}

    def act(self, state: CaseState, hour: int, transport: Transport) -> list[Action]:
        if state.case_id not in self.plans:
            plan = self.planner.plan(state)
            self.plans[state.case_id] = plan
            self._queue[state.case_id] = list(plan.actions)

        queue = self._queue.get(state.case_id, [])
        # ``<=`` rather than ``==``: an action whose hour was skipped because the case
        # was terminal and then reopened would otherwise sit in the queue forever. It
        # is still gated on the way out, so a stale action gets refused rather than
        # taken late.
        due = [a for a in queue if a.at_hour <= hour]
        if not due:
            return []

        self._queue[state.case_id] = [a for a in queue if a.at_hour > hour]
        taken: list[Action] = []
        for action in due:
            _, result = self.gate.execute(action, state)
            if result is not None:
                taken.append(action)
        return taken

    def stats(self) -> dict:
        from recovery.agent.runner import summarise

        return summarise(self.plans.values())
