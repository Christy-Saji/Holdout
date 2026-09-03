"""The policy engine: every money and contact action passes through here.

Two design commitments, both worth defending on video.

**The engine does not short-circuit.** Returning as soon as one rule denies would be
faster and would halve the value of the audit trail. Every rule runs on every action,
and the ledger records the complete verdict set -- pass and fail. ``Decision.allowed``
is the conjunction; ``Decision.verdicts`` is the whole list.

**Every evaluation is written to the ledger, allow and deny.** The denials are the
blocked-action log. An audit trail that records only what happened is half an audit
trail; what the system *refused to do* is the interesting half. :class:`Gate` is what
makes that structural rather than a convention an arm could forget to follow: it
evaluates, writes the ``policy_check`` entry, and only then touches the transport.

Rules see a :class:`~recovery.transport.base.CaseState`, never a ``Case``. A rule
therefore *cannot* read latents -- there is no field to read.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol, Sequence

from recovery.ledger import KIND_ACTION, KIND_POLICY_CHECK, Ledger, read_entries
from recovery.scoring import Scorer
from recovery.transport.base import (
    ACTION_CLOSE,
    ACTION_MESSAGE,
    ACTION_RETRY,
    TEMPLATE_POST_DEBIT_NOTICE,
    TEMPLATE_PRE_DEBIT_NOTICE,
    Action,
    CaseState,
    Result,
    SimParams,
    Transport,
)

NOTICE_TEMPLATES = frozenset({TEMPLATE_PRE_DEBIT_NOTICE, TEMPLATE_POST_DEBIT_NOTICE})


# -- verdicts and decisions ----------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    """One rule's opinion, with the values it actually looked at.

    ``evidence`` is not decoration. A verdict that says "denied" without saying what it
    saw is an assertion; one that carries the attempt count it compared against the cap,
    or the timestamp it compared against the notice window, is audit evidence.
    """

    rule_id: str
    allow: bool
    reason: str
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "allow": self.allow,
            "reason": self.reason,
            "evidence": dict(self.evidence),
        }


@dataclass(frozen=True)
class Decision:
    action: Action
    allowed: bool
    verdicts: tuple[Verdict, ...]

    def denials(self) -> tuple[Verdict, ...]:
        return tuple(v for v in self.verdicts if not v.allow)

    def first_denial(self) -> Verdict | None:
        """The first objecting rule in registration order. Used to explain a refusal
        to the agent; the full list still reaches the ledger."""
        denials = self.denials()
        return denials[0] if denials else None

    def to_dict(self) -> dict:
        return {
            "action": self.action.to_dict(),
            "allowed": self.allowed,
            "verdicts": [v.to_dict() for v in self.verdicts],
        }


# -- the derived read model of the ledger ---------------------------------------------


@dataclass
class LedgerIndex:
    """History the rules need, derived from the ledger as it is written.

    ``Idempotency``, ``TerminalState``, ``BudgetCeiling`` and ``ContactFrequency`` are
    the rules whose state lives in history rather than in the case, which is why
    ``evaluate()`` takes the ledger as a parameter. Scanning the ledger file on every
    evaluation would be quadratic over a 500-case run, so the gate maintains this index
    as it appends. :meth:`from_ledger_file` rebuilds it from a committed ledger, and a
    test asserts the two agree.
    """

    used_keys: set[str] = field(default_factory=set)
    case_spend: dict[str, int] = field(default_factory=dict)
    customer_contact_hours: dict[str, list[int]] = field(default_factory=dict)
    notices: dict[str, list[dict]] = field(default_factory=dict)

    def copy(self) -> "LedgerIndex":
        """An independent copy, for evaluating a plan that has not executed yet.

        The agent arm (phase 5) plans a whole case at hour 0: a pre-debit notice at
        hour 24, then the debit it announces at hour 48. Screening that debit against
        the *real* index would deny it, because at planning time the notice has not
        been sent. So the planner screens against a copy into which each approved step
        of its own plan is recorded, and the real index stays untouched until an action
        actually executes. Nothing here is shared with the original.
        """
        return LedgerIndex(
            used_keys=set(self.used_keys),
            case_spend=dict(self.case_spend),
            customer_contact_hours={k: list(v) for k, v in self.customer_contact_hours.items()},
            notices={k: [dict(n) for n in v] for k, v in self.notices.items()},
        )

    def record_execution(
        self,
        *,
        action: Action,
        state: CaseState,
        clock: datetime,
        cost_paise: int,
    ) -> None:
        """Only an action that actually executed consumes a key or spends money."""
        self.used_keys.add(action.idempotency_key)
        self.case_spend[action.case_id] = self.case_spend.get(action.case_id, 0) + cost_paise

        if action.type == ACTION_MESSAGE:
            if action.template not in NOTICE_TEMPLATES:
                self.customer_contact_hours.setdefault(state.customer_id, []).append(
                    action.at_hour
                )
            if action.template == TEMPLATE_PRE_DEBIT_NOTICE:
                self.record_notice(
                    case_id=action.case_id,
                    template=action.template,
                    sent_at=clock,
                    metadata=action.metadata,
                )

    def record_notice(self, *, case_id: str, template: str, sent_at: datetime, metadata: dict) -> None:
        self.notices.setdefault(case_id, []).append(
            {"template": template, "sent_at": sent_at, "metadata": dict(metadata)}
        )

    def notices_for(self, case_id: str, template: str) -> list[dict]:
        return [n for n in self.notices.get(case_id, ()) if n["template"] == template]

    def contact_hours(self, customer_id: str) -> list[int]:
        return self.customer_contact_hours.get(customer_id, [])

    def spend(self, case_id: str) -> int:
        return self.case_spend.get(case_id, 0)

    @classmethod
    def from_ledger_file(cls, path: Path | str) -> "LedgerIndex":
        """Rebuild the read model from a committed ledger."""
        index = cls()
        for row in read_entries(path):
            if row["kind"] != KIND_ACTION:
                continue
            action = row.get("action") or {}
            key = row.get("idempotency_key")
            if key:
                index.used_keys.add(key)
            case_id = row.get("case_id", "")
            index.case_spend[case_id] = index.case_spend.get(case_id, 0) + int(
                row.get("cost_paise", 0)
            )
            if action.get("type") == ACTION_MESSAGE:
                template = action.get("template")
                if template not in NOTICE_TEMPLATES:
                    customer = (row.get("result") or {}).get("customer_id", case_id)
                    index.customer_contact_hours.setdefault(customer, []).append(
                        int(action.get("at_hour", 0))
                    )
                if template == TEMPLATE_PRE_DEBIT_NOTICE:
                    index.record_notice(
                        case_id=case_id,
                        template=template,
                        sent_at=datetime.fromisoformat(row["ts"]),
                        metadata=action.get("metadata") or {},
                    )
        return index


# -- rules -------------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyContext:
    """Everything a rule may look at. Notably not a ``Case``, and so not a ``Latents``."""

    state: CaseState
    clock: datetime
    at_hour: int
    history: LedgerIndex
    scorer: Scorer
    params: SimParams


class Rule(Protocol):
    rule_id: str

    def check(self, action: Action, ctx: PolicyContext) -> Verdict: ...


def not_applicable(rule_id: str, why: str, **evidence) -> Verdict:
    """An allow that says the rule had no opinion.

    Rules that do not apply to an action still return a verdict, so
    ``Decision.verdicts`` always carries one entry per registered rule and the ledger
    shows the full rule set was consulted.
    """
    return Verdict(rule_id=rule_id, allow=True, reason=why, evidence={"applicable": False, **evidence})


# -- the engine --------------------------------------------------------------------


@dataclass(frozen=True)
class PolicyEngine:
    rules: tuple[Rule, ...]

    # The scorer and simulation parameters ride on the engine rather than on
    # evaluate()'s signature, so that signature stays exactly as specified:
    # evaluate(action, case, ledger, clock).
    _scorer: Scorer = field(default_factory=Scorer)
    _params: SimParams = field(default_factory=SimParams)

    def evaluate(
        self,
        action: Action,
        case: CaseState,
        ledger: LedgerIndex,
        clock: datetime,
    ) -> Decision:
        """Run **every** rule. No short-circuit, ever.

        ``case`` is the arm-visible state and ``ledger`` the derived read model of the
        audit trail. ``clock`` is the simulated clock at the moment of the action.
        """
        ctx = PolicyContext(
            state=case,
            clock=clock,
            at_hour=action.at_hour,
            history=ledger,
            scorer=self._scorer,
            params=self._params,
        )
        verdicts = tuple(rule.check(action, ctx) for rule in self.rules)
        return Decision(action=action, allowed=all(v.allow for v in verdicts), verdicts=verdicts)


def default_engine(scorer: Scorer | None = None, params: SimParams | None = None) -> PolicyEngine:
    """The twelve rules, in a fixed order.

    Order does not affect the outcome -- ``allowed`` is a conjunction -- but it fixes
    which denial ``first_denial()`` reports to the agent, so cheap structural refusals
    come before economic ones and the model gets the most actionable reason first.
    """
    from recovery.policy import rbi, rules as ops

    scorer = scorer or Scorer(params=params or SimParams())
    params = params or scorer.params
    registered: tuple[Rule, ...] = (
        ops.TerminalState(),
        ops.Idempotency(),
        ops.HardDeclineNoRetry(),
        ops.ActionDeclineNoRetry(),
        ops.CoolingOff(),
        ops.AttemptCap(),
        ops.ContactFrequency(),
        ops.QuietHours(),
        ops.BudgetCeiling(),
        rbi.RBIPreDebitNotice(),
        rbi.RBIAdditionalFactorAuth(),
        rbi.RBIPostDebitNotice(),
    )
    return PolicyEngine(rules=registered, _scorer=scorer, _params=params)


# -- the gate ----------------------------------------------------------------------


@dataclass
class Gate:
    """Evaluate, log, then act -- in that order, with no way round it.

    Every arm and every agent tool that moves money or contacts a customer goes through
    :meth:`execute`. The transport is only ever touched after a decision has been
    written to the ledger, so "every evaluation is logged" is a property of the code
    path rather than a discipline someone has to remember.
    """

    engine: PolicyEngine
    ledger: Ledger
    transport: Transport
    arm: str
    params: SimParams = field(default_factory=SimParams)
    index: LedgerIndex = field(default_factory=LedgerIndex)

    def clock_for(self, state: CaseState, at_hour: int) -> datetime:
        """The simulated clock. Never ``datetime.now()``."""
        return state.failed_at + timedelta(hours=at_hour)

    def preview(self, action: Action, state: CaseState, *, index: LedgerIndex | None = None) -> Decision:
        """A read-only dry run. Writes nothing and touches no transport state.

        This is what the agent's ``check_policy`` tool calls. Giving the model a
        dry-run gate means the sensible path is to plan within the constraints, so a
        denial on a real action becomes a genuine surprise worth logging rather than
        the model discovering the rules by bumping into them.

        ``index`` overrides the history the rules read. The agent's planner passes its
        provisional index -- see :meth:`LedgerIndex.copy` -- so that a dry run and the
        :meth:`screen` that follows it agree with each other.
        """
        return self.engine.evaluate(
            action, state, index or self.index, self.clock_for(state, action.at_hour)
        )

    def screen(self, action: Action, state: CaseState, *, index: LedgerIndex | None = None) -> Decision:
        """Evaluate and **record**, without touching the transport.

        This is what a gated agent tool calls when it is scheduling an action for a
        later hour rather than performing one now. A refusal here is a real refusal --
        it goes into the ledger as a ``policy_check`` with ``allowed=False`` and the
        action never gets queued -- while an approval is provisional, and the action is
        gated again by :meth:`execute` at the hour it actually fires.

        ``index`` overrides the history the rules read, for a planner screening a
        multi-step plan against its own provisional history.
        """
        clock = self.clock_for(state, action.at_hour)
        decision = self.engine.evaluate(action, state, index or self.index, clock)
        if not decision.allowed:
            self.ledger.append(
                ts=clock,
                arm=self.arm,
                case_id=action.case_id,
                kind=KIND_POLICY_CHECK,
                action=action.to_dict(),
                verdicts=[v.to_dict() for v in decision.verdicts],
                allowed=False,
                idempotency_key=action.idempotency_key,
                cost_paise=0,
            )
        return decision

    def execute(self, action: Action, state: CaseState) -> tuple[Decision, Result | None]:
        """Gate and perform. Returns the decision and the transport result, if any."""
        clock = self.clock_for(state, action.at_hour)
        decision = self.engine.evaluate(action, state, self.index, clock)

        self.ledger.append(
            ts=clock,
            arm=self.arm,
            case_id=action.case_id,
            kind=KIND_POLICY_CHECK,
            action=action.to_dict(),
            verdicts=[v.to_dict() for v in decision.verdicts],
            allowed=decision.allowed,
            idempotency_key=action.idempotency_key,
            cost_paise=0,
        )
        if not decision.allowed:
            return decision, None

        result = self._perform(action)
        self.index.record_execution(
            action=action, state=state, clock=clock, cost_paise=result.cost_paise
        )
        self.ledger.append(
            ts=clock,
            arm=self.arm,
            case_id=action.case_id,
            kind=KIND_ACTION,
            action=action.to_dict(),
            allowed=True,
            idempotency_key=action.idempotency_key,
            cost_paise=result.cost_paise,
            result=result.to_dict(),
        )
        return decision, result

    def _perform(self, action: Action) -> Result:
        if action.type == ACTION_RETRY:
            return self.transport.attempt_retry(
                action.case_id, action.at_hour, action.idempotency_key
            )
        if action.type == ACTION_MESSAGE:
            return self.transport.send_message(
                action.case_id,
                action.channel or "",
                action.template or "",
                action.at_hour,
                action.idempotency_key,
            )
        if action.type == ACTION_CLOSE:
            return self.transport.close_case(
                action.case_id, action.at_hour, str(action.metadata.get("reason", "closed"))
            )
        raise ValueError(f"no transport path for action type {action.type!r}")


def rules_summary(engine: PolicyEngine) -> Sequence[str]:
    """One line per rule. Goes into the agent's cached system prompt in phase 5."""
    return [f"{rule.rule_id}: {getattr(rule, 'summary', '')}" for rule in engine.rules]
