"""The holdout control arm, and the interface every arm implements.

The control arm does nothing. That is not a placeholder -- **it is the measurement
instrument.** The simulator still runs the self-heal hazard against every case, so the
control arm recovers real money without anyone lifting a finger, and that number is
the counterfactual which turns every other arm's gross recovery into an *incremental*
one.

It gets exactly the same ledger treatment as the arms that act, so the run artifacts
are symmetric and a reviewer can diff them.

The ``Arm`` protocol lives here because the control arm is its reference
implementation: an arm receives an :class:`~recovery.transport.base.CaseState` -- which
contains no latents -- plus the hour and a transport, and returns the actions it took.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from recovery.ledger import Ledger
from recovery.transport.base import Action, CaseState, SimParams, Transport

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance, phase 4 onward
    from recovery.policy.engine import Gate
    from recovery.scoring import Scorer


@dataclass(frozen=True)
class ArmContext:
    """Everything the harness hands an arm at construction time.

    ``gate`` and ``scorer`` are ``None`` in phase 3 because the control arm takes no
    actions and therefore needs no policy gate. From phase 4 onward every arm that
    acts receives both, and every money or contact action goes through the gate.
    """

    arm: str
    run_id: str
    seed: int
    params: SimParams
    transport: Transport
    ledger: Ledger
    gate: "Gate | None" = None
    scorer: "Scorer | None" = None
    options: dict = field(default_factory=dict)
    """Arm-specific switches from the CLI -- cassette mode for the agent arm, say."""


class Arm(Protocol):
    """A recovery policy. Sees only what ``observe()`` returns."""

    name: str

    def act(self, state: CaseState, hour: int, transport: Transport) -> list[Action]:
        """Return the actions taken at this hour. An empty list means 'do nothing'."""
        ...


class ControlArm:
    """Does nothing, on purpose, for all 336 hours.

    Every rupee this arm recovers is a rupee no intervention was responsible for.
    Subtracting it is the difference between a headline number that means something
    and one that does not.
    """

    name = "control"

    def __init__(self, ctx: ArmContext | None = None) -> None:
        self.ctx = ctx

    def act(self, state: CaseState, hour: int, transport: Transport) -> list[Action]:
        return []
