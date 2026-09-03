"""The three-arm runner.

Loads the cohort once, then runs each arm over the full 336-hour window against its
**own** transport instance. Arms never share mutable state: a transport shared between
arms would let arm two inherit arm one's attempt history, which is the failure mode
that produces a beautiful, wrong number.

One ledger per ``(run_id, arm)`` at ``data/results/<run_id>/<arm>.jsonl``. That single
hash-chained file is both the audit trail and the results file -- policy checks and
actions are written by the gate as they happen (phase 4 onward), and one ``outcome``
entry per case is written when the window closes. Phase 6 bootstraps from those
``outcome`` entries rather than re-running the simulation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Sequence

from recovery.cohort import Case, generate_cohort
from recovery.ledger import KIND_OUTCOME, Ledger, verify
from recovery.transport.base import SimParams
from recovery.transport.mock import CaseOutcome, MockTransport

DEFAULT_RESULTS_ROOT = Path("data/results")
CONTROL_ARM = "control"
KNOWN_ARMS = ("control", "baseline", "agent")


def default_run_id(seed: int, n: int) -> str:
    """Deterministic, so re-running overwrites rather than accumulating directories.

    A wall-clock or uuid run id would make every rerun a new directory and every
    committed figure a diff. The reproduction gate wants the opposite.
    """
    return f"run-s{seed}-n{n}"


@dataclass(frozen=True)
class ArmRun:
    arm: str
    ledger_path: Path
    outcomes: tuple[CaseOutcome, ...]
    stats: dict = field(default_factory=dict)
    """Arm-specific counters. The agent arm reports turns, tokens and cache hits here;
    every other arm reports nothing, because there is nothing to report."""

    @property
    def by_case(self) -> dict[str, CaseOutcome]:
        return {o.case_id: o for o in self.outcomes}


@dataclass(frozen=True)
class RunResult:
    run_id: str
    seed: int
    n: int
    params: SimParams
    run_dir: Path
    arms: tuple[ArmRun, ...]

    @property
    def by_arm(self) -> dict[str, ArmRun]:
        return {a.arm: a for a in self.arms}

    @property
    def case_ids(self) -> tuple[str, ...]:
        return tuple(o.case_id for o in self.arms[0].outcomes) if self.arms else ()


def build_arm(name: str, ctx) -> object:
    """Construct an arm by name.

    Imports are local so that phase 3 can run with no policy engine on disk and so
    that ``recovery.arms.agent`` -- which imports ``anthropic`` -- is never imported
    for a run that does not include the agent arm.
    """
    if name == "control":
        from recovery.arms.control import ControlArm

        return ControlArm(ctx)
    if name == "baseline":
        from recovery.arms.baseline import BaselineArm

        return BaselineArm(ctx)
    if name == "agent":
        from recovery.arms.agent import AgentArm

        return AgentArm(ctx)
    raise ValueError(f"unknown arm {name!r}; expected one of {KNOWN_ARMS}")


def run(
    seed: int = 42,
    n: int = 500,
    arms: Sequence[str] = (CONTROL_ARM,),
    *,
    params: SimParams | None = None,
    out_root: Path | str = DEFAULT_RESULTS_ROOT,
    run_id: str | None = None,
    cases: Sequence[Case] | None = None,
    agent_options: dict | None = None,
) -> RunResult:
    """Run every named arm over the same cohort and write one ledger per arm."""
    params = params or SimParams()
    cases = list(cases) if cases is not None else generate_cohort(seed=seed, n=n)
    run_id = run_id or default_run_id(seed, len(cases))

    run_dir = Path(out_root) / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    arm_runs: list[ArmRun] = []
    for arm_name in arms:
        arm_runs.append(
            _run_one_arm(
                arm_name,
                cases=cases,
                seed=seed,
                params=params,
                run_id=run_id,
                run_dir=run_dir,
                agent_options=agent_options,
            )
        )

    _write_manifest(run_dir, run_id=run_id, seed=seed, n=len(cases), arms=arms, params=params)
    return RunResult(
        run_id=run_id,
        seed=seed,
        n=len(cases),
        params=params,
        run_dir=run_dir,
        arms=tuple(arm_runs),
    )


def _run_one_arm(
    arm_name: str,
    *,
    cases: Sequence[Case],
    seed: int,
    params: SimParams,
    run_id: str,
    run_dir: Path,
    agent_options: dict | None,
) -> ArmRun:
    from recovery.arms.control import ArmContext

    ledger_path = run_dir / f"{arm_name}.jsonl"
    # A fresh run starts a fresh chain. The Ledger class itself has no update or
    # delete path; replacing the previous run's file is the harness's business, not
    # the ledger's, and it is what keeps a rerun byte-comparable with its predecessor.
    ledger_path.unlink(missing_ok=True)

    transport = MockTransport(cases, seed=seed, params=params)
    ledger = Ledger(ledger_path, run_id=run_id)

    gate = None
    scorer = None
    if arm_name != CONTROL_ARM:
        # The control arm takes no actions and so needs no gate. Every arm that does
        # act gets one, and every money or contact action it takes goes through it.
        from recovery.policy.engine import Gate, default_engine
        from recovery.scoring import Scorer

        scorer = Scorer(params=params)
        gate = Gate(
            engine=default_engine(scorer),
            ledger=ledger,
            transport=transport,
            arm=arm_name,
            params=params,
        )

    ctx = ArmContext(
        arm=arm_name,
        run_id=run_id,
        seed=seed,
        params=params,
        transport=transport,
        ledger=ledger,
        gate=gate,
        scorer=scorer,
        options=dict(agent_options or {}),
    )
    arm = build_arm(arm_name, ctx)

    try:
        drive(arm, transport, cases, params)
        _write_outcomes(ledger, arm_name, transport, cases, params)
    finally:
        ledger.close()

    stats_fn = getattr(arm, "stats", None)
    return ArmRun(
        arm=arm_name,
        ledger_path=ledger_path,
        outcomes=transport.outcomes(),
        stats=stats_fn() if callable(stats_fn) else {},
    )


def drive(arm, transport: MockTransport, cases: Sequence[Case], params: SimParams) -> None:
    """The hourly loop. Sorted case order, terminal cases dropped as they close."""
    open_ids = sorted(c.case_id for c in cases)
    for hour in range(params.window_h):
        transport.tick(hour)
        still_open: list[str] = []
        for case_id in open_ids:
            state = transport.observe(case_id, hour)
            if state.is_terminal:
                continue
            arm.act(state, hour, transport)
            still_open.append(case_id)
        open_ids = still_open
        if not open_ids:
            break


def _write_outcomes(
    ledger: Ledger,
    arm: str,
    transport: MockTransport,
    cases: Sequence[Case],
    params: SimParams,
) -> None:
    """One ``outcome`` entry per case, timestamped at the close of its own window."""
    failed_at = {c.case_id: c.failed_at for c in cases}
    for outcome in transport.outcomes():
        ledger.append(
            ts=failed_at[outcome.case_id] + timedelta(hours=params.window_h),
            arm=arm,
            case_id=outcome.case_id,
            kind=KIND_OUTCOME,
            cost_paise=outcome.cost_paise,
            result=outcome.to_dict(),
        )


def _write_manifest(
    run_dir: Path, *, run_id: str, seed: int, n: int, arms: Sequence[str], params: SimParams
) -> None:
    """Run parameters, so a reviewer can see exactly what produced these numbers.

    Deterministic by construction -- no wall clock anywhere in it.
    """
    manifest = {
        "run_id": run_id,
        "seed": seed,
        "n": n,
        "arms": list(arms),
        "params": params.to_dict(),
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )


# -- reading a completed run back ---------------------------------------------------


def load_run(run_dir: Path | str) -> RunResult:
    """Rebuild a :class:`RunResult` from a run directory, without re-simulating."""
    run_dir = Path(run_dir)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    costs_d = manifest["params"]["costs"]
    from recovery.transport.base import CostModel

    params = SimParams(
        window_h=manifest["params"]["window_h"],
        p_self_heal_scale=manifest["params"]["p_self_heal_scale"],
        attempt_decay=tuple(manifest["params"]["attempt_decay"]),
        costs=CostModel(**costs_d),
    )

    arm_runs: list[ArmRun] = []
    for arm_name in manifest["arms"]:
        path = run_dir / f"{arm_name}.jsonl"
        if not path.exists():
            continue
        arm_runs.append(
            ArmRun(arm=arm_name, ledger_path=path, outcomes=tuple(read_outcomes(path)))
        )

    return RunResult(
        run_id=manifest["run_id"],
        seed=manifest["seed"],
        n=manifest["n"],
        params=params,
        run_dir=run_dir,
        arms=tuple(arm_runs),
    )


def read_outcomes(ledger_path: Path | str) -> list[CaseOutcome]:
    """Per-case outcomes, read back from the ledger's ``outcome`` entries."""
    from recovery.ledger import read_entries

    return [
        CaseOutcome.from_dict(row["result"])
        for row in read_entries(ledger_path)
        if row["kind"] == KIND_OUTCOME
    ]


def verify_run(result: RunResult) -> dict[str, bool]:
    """Chain-verify every arm's ledger. Reported by the CLI after each run."""
    return {a.arm: verify(a.ledger_path) for a in result.arms}
