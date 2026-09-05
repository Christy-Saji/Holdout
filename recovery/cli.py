"""Command-line entry point for the recovery evaluation harness.

    python -m recovery.cli eval --seed 42 --n 500 --arms control,baseline,agent
    python -m recovery.cli report
    python -m recovery.cli sweep --param p_self_heal --range 0.5,1.5
    python -m recovery.cli cohort --seed 42 --n 500
    python -m recovery.cli repro
"""

from __future__ import annotations

import argparse
from pathlib import Path

from recovery import cohort as cohort_mod
from recovery.cohort import Case


def _build_cohort(seed: int, n: int, out: str | None = None) -> tuple[Path, list[Case]]:
    cases = cohort_mod.generate_cohort(seed=seed, n=n)
    path = cohort_mod.write_cohort(cases, out or cohort_mod.cohort_path_for(seed))
    print(f"cohort: {n} cases (seed={seed}) -> {path}")
    print(f"latents side table (simulator only) -> {cohort_mod.latents_path_for(path)}")
    return path, cases


def _cohort_for_eval(seed: int, n: int) -> list[Case]:
    """The cohort ``eval`` runs on, without ever shrinking the committed artifact.

    ``eval`` is meant to be self-contained -- a clean clone runs one command and gets
    byte-identical input -- so it writes the artifact when there is none. What it must
    **not** do is overwrite an existing one at a different size: developing against
    ``--n 20`` would silently replace the committed 500-case cohort, and the
    reproduction gate would then pass against the wrong input while reporting a
    different headline number. The cohort is a pure function of ``(seed, n)``, so the
    run itself is unaffected either way; only the artifact on disk is at risk.
    """
    cases = cohort_mod.generate_cohort(seed=seed, n=n)
    path = cohort_mod.cohort_path_for(seed)

    if not path.exists():
        cohort_mod.write_cohort(cases, path)
        print(f"cohort: {n} cases (seed={seed}) -> {path}")
        print(f"latents side table (simulator only) -> {cohort_mod.latents_path_for(path)}")
        return cases

    existing = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
    if existing != n:
        print(
            f"cohort: running {n} cases (seed={seed}) in memory; left {path} alone, "
            f"because it holds {existing} and eval does not overwrite a cohort artifact "
            f"at a different size. Use `cohort --seed {seed} --n {n}` to replace it."
        )
    else:
        print(f"cohort: {n} cases (seed={seed}) <- {path}")
    return cases


def _cmd_cohort(args: argparse.Namespace) -> int:
    _build_cohort(args.seed, args.n, args.out)
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from recovery.eval import harness, metrics

    cases = _cohort_for_eval(args.seed, args.n)

    agent_options = {"mode": "record" if args.record else ("live" if args.live else "replay")}
    try:
        result = harness.run(
            seed=args.seed,
            n=args.n,
            arms=args.arms,
            cases=cases,
            agent_options=agent_options,
        )
    except Exception as exc:  # noqa: BLE001 - re-raised unless it is one we can explain
        if (explained := _explain_agent_failure(exc)) is None:
            raise
        print(explained)
        return 2

    computed = metrics.compute(result)
    print()
    print(f"run_id: {result.run_id}   ->  {result.run_dir}")
    print()
    print(metrics.format_table(computed))
    print()
    print(metrics.format_blocked(computed))
    print()
    for arm_run in result.arms:
        if arm_run.stats:
            print(f"{arm_run.arm} arm:")
            for key, value in sorted(arm_run.stats.items()):
                print(f"  {key:<20} {value}")
            print()
    for arm, ok in harness.verify_run(result).items():
        print(f"ledger chain {arm:<10} {'verified' if ok else 'BROKEN'}")
    return 0


def _explain_agent_failure(exc: Exception) -> str | None:
    """Turn the two agent-arm failures a reviewer will actually hit into advice.

    A traceback is the right output for a bug. Neither of these is a bug: one is a
    cassette that was never recorded, the other is a recording run with no credential.
    """
    from recovery.agent.runner import CassetteMiss

    if isinstance(exc, CassetteMiss):
        return (
            f"\nagent arm: {exc}\n\n"
            "Replay found no recorded decision for a case. Either the cassettes were "
            "never recorded, or the prompt, the tool schemas or a tool result changed "
            "since they were. Re-record with:\n\n"
            "    python -m recovery.cli eval --seed 42 --n 50 --arms agent --record\n\n"
            "Replay deliberately does not fall through to a live API call."
        )
    if type(exc).__name__ == "AnthropicError" and "api_key" in str(exc):
        return (
            f"\nagent arm: {exc}\n\n"
            "A recording or live run needs a credential; replay does not. Set "
            "ANTHROPIC_API_KEY, or drop --record/--live to run from the committed "
            "cassettes."
        )
    return None


def _cmd_report(args: argparse.Namespace) -> int:
    from recovery.eval import report

    return report.main(run_id=args.run_id, results_root=Path(args.results_root), figures=not args.no_figures)


def _cmd_sweep(args: argparse.Namespace) -> int:
    from recovery.eval import report

    return report.sweep_main(
        param=args.param,
        span=args.range,
        points=args.points,
        seed=args.seed,
        n=args.n,
        arms=args.arms,
    )


def _cmd_repro(args: argparse.Namespace) -> int:
    from recovery.eval import repro

    if args.in_process:
        return repro.main(
            run_id=args.run_id,
            results_root=Path(args.results_root),
            cohort_path=args.cohort,
        )
    return repro.clean_clone(
        run_id=args.run_id,
        results_root=Path(args.results_root),
        worktree=args.worktree,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recovery", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_eval = sub.add_parser("eval", help="run the evaluation over one or more arms")
    p_eval.add_argument("--seed", type=int, default=42)
    p_eval.add_argument("--n", type=int, default=500)
    p_eval.add_argument("--arms", type=lambda s: s.split(","), default=["control"])
    p_eval.add_argument("--record", action="store_true", help="agent arm: re-record cassettes")
    p_eval.add_argument("--live", action="store_true", help="agent arm: bypass cassettes")
    p_eval.set_defaults(func=_cmd_eval)

    p_cohort = sub.add_parser("cohort", help="regenerate the synthetic cohort artifact")
    p_cohort.add_argument("--seed", type=int, default=42)
    p_cohort.add_argument("--n", type=int, default=500)
    p_cohort.add_argument(
        "--out", default=None, help="output path (default data/cohort_seed<seed>.jsonl)"
    )
    p_cohort.set_defaults(func=_cmd_cohort)

    p_report = sub.add_parser("report", help="summarise a completed run")
    p_report.add_argument("--run-id", default=None)
    p_report.add_argument("--results-root", default="data/results")
    p_report.add_argument("--no-figures", action="store_true")
    p_report.set_defaults(func=_cmd_report)

    p_sweep = sub.add_parser("sweep", help="sensitivity sweep over a simulation parameter")
    p_sweep.add_argument("--param", type=str, required=True)
    p_sweep.add_argument("--range", type=lambda s: [float(x) for x in s.split(",")], required=True)
    p_sweep.add_argument("--points", type=int, default=5)
    p_sweep.add_argument("--seed", type=int, default=42)
    p_sweep.add_argument("--n", type=int, default=500)
    p_sweep.add_argument(
        "--arms",
        type=lambda s: s.split(","),
        default=["control", "baseline"],
        help="sweeps default to control,baseline so a swept parameter can never cause "
        "a cassette miss that falls through to a live API call",
    )
    p_sweep.set_defaults(func=_cmd_sweep)

    p_repro = sub.add_parser("repro", help="clean-clone reproduction gate")
    p_repro.add_argument("--run-id", default=None)
    p_repro.add_argument("--results-root", default="data/results")
    p_repro.add_argument(
        "--cohort", default=None, help="cohort artifact (default data/cohort_seed<seed>.jsonl)"
    )
    p_repro.add_argument(
        "--in-process",
        action="store_true",
        help="fast path: re-derive in this interpreter instead of cloning. Cannot catch "
        "a dependency missing from pyproject.toml, because this venv already has it.",
    )
    p_repro.add_argument(
        "--worktree",
        action="store_true",
        help="materialise the working tree instead of git HEAD, for checking a "
        "reproduction before the work that produces it has been committed.",
    )
    p_repro.set_defaults(func=_cmd_repro)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
