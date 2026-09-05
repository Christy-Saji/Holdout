"""Reporting and sensitivity analysis over completed runs.

Two entry points, both reached from ``recovery.cli``:

``main``
    Summarise a run that already exists on disk. It re-reads the ledgers rather than
    re-simulating, so the reported numbers are the committed artifacts' numbers by
    construction -- a report that quietly re-ran the simulation could disagree with
    the files it claims to describe and nobody would notice.

``sweep_main``
    Re-run the evaluation across a range of a simulation parameter and report how the
    headline moves. **The question a sweep answers is not "how big is the lift" but
    "does the lift survive".** A sign that flips inside a plausible parameter range is
    a stated limitation, not a defect to tune away, and this module reports it as such.

All money stays integer paise until :func:`recovery.eval.metrics.rupees` at the
display boundary. No wall clock anywhere: tables and figures are a pure function of
the run directory.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Sequence

from recovery.eval import harness, metrics

DEFAULT_RESULTS_ROOT = Path("data/results")
SWEEP_DIRNAME = "sweeps"

SWEEPABLE = {
    "p_self_heal": "p_self_heal_scale",
    "p_self_heal_scale": "p_self_heal_scale",
    "retry_cost": "costs.retry_paise",
    "retry_paise": "costs.retry_paise",
    "sms_paise": "costs.sms_paise",
    "whatsapp_paise": "costs.whatsapp_paise",
    "email_paise": "costs.email_paise",
    "attempt_decay": "attempt_decay",
}
"""Parameters a sweep may vary.

A sweep range is always a **multiplier on the published value**, so ``--range 0.5,1.5``
means the same thing whichever parameter is named and the published run is always the
``x1.0`` point on every chart.
"""


# -- report -------------------------------------------------------------------------


def resolve_run_dir(run_id: str | None, results_root: Path | str) -> Path:
    """Find the run to report on, or say clearly why it cannot be found."""
    root = Path(results_root)
    if run_id:
        run_dir = root / run_id
        if not (run_dir / "manifest.json").exists():
            raise SystemExit(
                f"no run at {run_dir} (expected a manifest.json there). "
                "Run `python -m recovery.cli eval` first."
            )
        return run_dir

    candidates = sorted(
        p.parent for p in root.glob("*/manifest.json") if p.parent.name != SWEEP_DIRNAME
    )
    if not candidates:
        raise SystemExit(
            f"no completed runs under {root}. Run `python -m recovery.cli eval "
            "--seed 42 --n 500 --arms control,baseline` first."
        )
    if len(candidates) > 1:
        names = ", ".join(p.name for p in candidates)
        raise SystemExit(f"several runs under {root} ({names}); pass --run-id to choose one")
    return candidates[0]


def main(
    run_id: str | None = None,
    results_root: Path | str = DEFAULT_RESULTS_ROOT,
    figures: bool = True,
) -> int:
    """Print the results table, the bootstrap intervals and the blocked-action log."""
    run_dir = resolve_run_dir(run_id, results_root)
    run = harness.load_run(run_dir)
    computed = metrics.compute(run)
    intervals = metrics.confidence_intervals(run)

    print()
    print(f"run_id: {run.run_id}   <-  {run_dir}")
    print(f"cohort: {run.n} cases (seed={run.seed}), {run.params.window_h}-hour window")
    print()
    print(metrics.format_table(computed))
    print()
    print(f"bootstrap: {metrics.BOOTSTRAP_REPLICATES} paired replicates, seed {run.seed}")
    print(metrics.format_intervals(intervals))
    print()
    print(metrics.format_fatigue(computed))
    print()
    print(metrics.format_blocked(computed))
    print()

    for arm, ok in harness.verify_run(run).items():
        print(f"ledger chain {arm:<10} {'verified' if ok else 'BROKEN'}")

    if figures:
        print()
        written = write_figures(run, computed, intervals)
        if not written:
            print("figures skipped (matplotlib unavailable)")
        for path in written:
            print(f"figure -> {path}")
    return 0


# -- sweep --------------------------------------------------------------------------


@dataclass(frozen=True)
class SweepPoint:
    multiplier: float
    value: float
    incremental_paise: int
    ci: metrics.ConfidenceInterval
    cost_per_incremental_rupee: float | None


def apply_multiplier(params, target: str, multiplier: float):
    """Return a copy of ``params`` with one field scaled by ``multiplier``."""
    if target.startswith("costs."):
        field = target.split(".", 1)[1]
        scaled = replace(
            params.costs, **{field: int(round(getattr(params.costs, field) * multiplier))}
        )
        return replace(params, costs=scaled)
    if target == "attempt_decay":
        # Scaling the whole curve preserves its monotonicity, which the simulator
        # relies on; clamping at 1.0 keeps it a probability multiplier.
        curve = tuple(min(1.0, d * multiplier) for d in params.attempt_decay)
        return replace(params, attempt_decay=curve)
    return replace(params, **{target: getattr(params, target) * multiplier})


def read_value(params, target: str) -> float:
    """The scalar shown in the sweep table for a given parameter."""
    if target.startswith("costs."):
        return getattr(params.costs, target.split(".", 1)[1])
    if target == "attempt_decay":
        return params.attempt_decay[1]
    return getattr(params, target)


def sweep_main(
    param: str,
    span: Sequence[float],
    points: int = 5,
    seed: int = 42,
    n: int = 500,
    arms: Sequence[str] = ("control", "baseline"),
    results_root: Path | str = DEFAULT_RESULTS_ROOT,
    figures: bool = True,
) -> int:
    """Re-run the evaluation across a parameter range and report sign survival."""
    if param not in SWEEPABLE:
        raise SystemExit(f"cannot sweep {param!r}; try one of {', '.join(sorted(SWEEPABLE))}")
    if "agent" in arms:
        raise SystemExit(
            "the agent arm cannot be swept: every sweep point changes the tool results "
            "the model saw, so every point would be a cassette miss. Sweep control,baseline."
        )
    if len(span) != 2 or span[0] >= span[1]:
        raise SystemExit(f"--range wants lo,hi with lo < hi; got {list(span)}")
    if points < 2:
        raise SystemExit("--points must be at least 2")

    from recovery.cohort import generate_cohort
    from recovery.transport.base import SimParams

    target = SWEEPABLE[param]
    cases = generate_cohort(seed=seed, n=n)
    base = SimParams()
    out_root = Path(results_root) / SWEEP_DIRNAME / param

    lo, hi = float(span[0]), float(span[1])
    step = (hi - lo) / (points - 1)
    multipliers = [lo + step * i for i in range(points)]
    treatment = next((a for a in arms if a != "control"), None)
    if treatment is None:
        raise SystemExit("a sweep needs a treatment arm alongside control")

    print()
    print(f"sweeping {param} ({target}) over x{lo:g}..x{hi:g} at {points} points")
    print(f"cohort: {n} cases (seed={seed}); arms: {','.join(arms)}")
    print(f"the published run sits at x1.0; sweep summary -> {out_root}")
    print()

    # A sweep point's ledgers are intermediate: the artifact a sweep produces is the
    # curve and the summary, not five more copies of the run. Writing them under
    # data/results/ would add ~20 MB of derived files to the repository for nothing.
    results: list[SweepPoint] = []
    with tempfile.TemporaryDirectory(prefix="recovery-sweep-") as scratch:
        for multiplier in multipliers:
            params = apply_multiplier(base, target, multiplier)
            run = harness.run(
                seed=seed,
                n=n,
                arms=arms,
                params=params,
                cases=cases,
                out_root=Path(scratch),
                run_id=f"x{multiplier:g}".replace(".", "_"),
            )
            control = run.by_arm["control"].outcomes
            outcomes = run.by_arm[treatment].outcomes
            point = SweepPoint(
                multiplier=multiplier,
                value=read_value(params, target),
                incremental_paise=metrics.incremental_recovery_paise(outcomes, control),
                ci=metrics.bootstrap_incremental(outcomes, control, seed=seed),
                cost_per_incremental_rupee=metrics.cost_per_incremental_rupee(outcomes, control),
            )
            results.append(point)
            print(f"  x{multiplier:<6.3g} -> {metrics.rupees(point.incremental_paise)}")

    print()
    print(format_sweep(results, param=param))

    out_root.mkdir(parents=True, exist_ok=True)
    summary = write_sweep_summary(results, param, target, seed, n, arms, out_root)
    print()
    print(f"summary -> {summary}")
    if figures:
        path = write_sweep_figure(results, param, out_root)
        if path:
            print(f"figure -> {path}")
    return 0


def write_sweep_summary(
    results: Sequence[SweepPoint],
    param: str,
    target: str,
    seed: int,
    n: int,
    arms: Sequence[str],
    out_dir: Path,
) -> Path:
    """The committable artifact of a sweep: the numbers, not the ledgers behind them."""
    payload = {
        "param": param,
        "target": target,
        "seed": seed,
        "n": n,
        "arms": list(arms),
        "sign_survives": all(r.incremental_paise > 0 for r in results),
        "points": [
            {
                "multiplier": r.multiplier,
                "value": r.value,
                "incremental_paise": r.incremental_paise,
                "ci": r.ci.to_dict(),
                "cost_per_incremental_rupee": r.cost_per_incremental_rupee,
            }
            for r in results
        ],
    }
    path = out_dir / f"sweep-{param}.json"
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    return path


def format_sweep(results: Sequence[SweepPoint], *, param: str) -> str:
    """The sweep table, ending in the only line that matters: does the sign survive."""
    header = f"{'x':>8} {'value':>12} {'incremental':>16} {'95% interval':>30} {'cost/incr Rs':>13}"
    lines = [header, "-" * len(header)]
    for r in results:
        span = f"[{metrics.rupees(r.ci.lo)}, {metrics.rupees(r.ci.hi)}]"
        cpi = (
            "n/a"
            if r.cost_per_incremental_rupee is None
            else f"{r.cost_per_incremental_rupee:.3f}"
        )
        lines.append(
            f"{r.multiplier:>8.3g} {r.value:>12.6g} "
            f"{metrics.rupees(r.incremental_paise):>16} {span:>30} {cpi:>13}"
        )

    positive = [r for r in results if r.incremental_paise > 0]
    confident = [r for r in results if r.ci.excludes_zero and r.ci.lo > 0]
    lines.append("")
    if len(positive) == len(results):
        lines.append(
            f"sign survives: incremental recovery stays positive at every {param} tested."
        )
    else:
        flipped = ", ".join(f"x{r.multiplier:g}" for r in results if r.incremental_paise <= 0)
        lines.append(
            f"SIGN DOES NOT SURVIVE: incremental recovery is <= 0 at {flipped}. "
            "This is a stated limitation of the result, not a defect to tune away."
        )
    lines.append(f"the interval excludes zero at {len(confident)}/{len(results)} points.")
    return "\n".join(lines)


# -- figures ------------------------------------------------------------------------


def _pyplot():
    """Matplotlib with a headless backend, or ``None`` if it is unavailable.

    Figures are a convenience for the README and the video. A missing plotting library
    must never take down the numbers, so this degrades to no figures rather than to a
    traceback in the middle of a report.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        return plt
    except Exception:  # noqa: BLE001 - any import or backend failure is non-fatal here
        return None


def write_figures(run, computed, intervals) -> list[Path]:
    """Three charts: recovery by arm, the interval on the headline, and the denials.

    Three is the ceiling on purpose. Every figure that does not go on screen in the
    five-minute video is time not spent on the ones that do.
    """
    plt = _pyplot()
    if plt is None:
        return []

    written: list[Path] = []

    fig, ax = plt.subplots(figsize=(7, 4))
    names = [m.arm for m in computed]
    x = range(len(names))
    ax.bar([i - 0.2 for i in x], [m.gross_paise / 100.0 for m in computed], width=0.4,
           label="gross recovered")
    ax.bar([i + 0.2 for i in x], [m.incremental_paise / 100.0 for m in computed], width=0.4,
           label="incremental over control")
    ax.set_xticks(list(x))
    ax.set_xticklabels(names)
    ax.set_ylabel("rupees")
    ax.set_title(f"Recovery by arm ({run.n} cases, seed {run.seed})")
    ax.legend()
    fig.tight_layout()
    path = run.run_dir / "recovery-by-arm.png"
    fig.savefig(path, dpi=140)
    plt.close(fig)
    written.append(path)

    treatments = [m.arm for m in computed if not m.is_control and m.arm in intervals]
    if treatments:
        arm = treatments[0]
        ci = intervals[arm]
        fig, ax = plt.subplots(figsize=(7, 3.2))
        ax.axvspan(ci.lo / 100.0, ci.hi / 100.0, alpha=0.25,
                   label=f"{int(ci.level * 100)}% interval")
        ax.axvline(ci.point / 100.0, linewidth=2, label="point estimate")
        ax.axvline(0, linewidth=1, linestyle="--", color="k", label="zero")
        ax.set_yticks([])
        ax.set_xlabel("incremental recovery (rupees)")
        ax.set_title(f"{arm} over control - {ci.replicates} paired bootstrap replicates")
        ax.legend(loc="upper left", fontsize=8)
        fig.tight_layout()
        path = run.run_dir / "incremental-interval.png"
        fig.savefig(path, dpi=140)
        plt.close(fig)
        written.append(path)

    blocking = [m for m in computed if m.blocked_by_rule]
    if blocking:
        m = blocking[-1]
        rules = sorted(m.blocked_by_rule.items(), key=lambda kv: kv[1])
        fig, ax = plt.subplots(figsize=(7, 0.5 * len(rules) + 1.8))
        ax.barh([r for r, _ in rules], [c for _, c in rules])
        for i, (_, count) in enumerate(rules):
            ax.text(count, i, f" {count}", va="center", fontsize=10)
        ax.set_xlim(0, max(c for _, c in rules) * 1.15)
        ax.set_xlabel("actions denied")
        ax.set_title(f"Blocked actions by rule - {m.arm} arm ({m.blocked_actions} total)")
        fig.tight_layout()
        path = run.run_dir / "blocked-by-rule.png"
        fig.savefig(path, dpi=140)
        plt.close(fig)
        written.append(path)

    return written


def write_sweep_figure(results: Sequence[SweepPoint], param: str, out_dir: Path) -> Path | None:
    """Incremental recovery against the swept parameter, with the zero line drawn."""
    plt = _pyplot()
    if plt is None:
        return None

    xs = [r.multiplier for r in results]
    fig, ax = plt.subplots(figsize=(7, 4))
    ax.fill_between(xs, [r.ci.lo / 100.0 for r in results], [r.ci.hi / 100.0 for r in results],
                    alpha=0.25, label="95% interval")
    ax.plot(xs, [r.incremental_paise / 100.0 for r in results], marker="o",
            label="incremental recovery")
    ax.axhline(0, linewidth=1, linestyle="--", color="k", label="zero")
    ax.axvline(1.0, linewidth=1, linestyle=":", color="grey", label="published run")
    ax.set_xlabel(f"{param} multiplier")
    ax.set_ylabel("incremental recovery (rupees)")
    ax.set_title(f"Sensitivity of the headline to {param}")
    ax.legend(fontsize=8)
    fig.tight_layout()
    path = out_dir / f"sweep-{param}.png"
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path
