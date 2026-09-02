"""Command-line entry point for the recovery evaluation harness.

This phase wires the argument parser and leaves each handler as a stub that
reports what it would do and exits 0. Later phases fill the handlers in.
"""

from __future__ import annotations

import argparse

from recovery import cohort as cohort_mod


def _build_cohort(seed: int, n: int, out: str | None = None) -> int:
    path = cohort_mod.build_cohort_artifact(seed=seed, n=n, out=out)
    latents = cohort_mod.latents_path_for(path)
    print(f"cohort: {n} cases (seed={seed}) -> {path}")
    print(f"latents side table (simulator only) -> {latents}")
    return 0


def _cmd_cohort(args: argparse.Namespace) -> int:
    return _build_cohort(args.seed, args.n, args.out)


def _cmd_eval(args: argparse.Namespace) -> int:
    # The cohort is regenerated as a side effect so that `eval` is self-contained:
    # a reviewer with a clean clone runs one command and gets byte-identical input.
    _build_cohort(args.seed, args.n)
    print(
        "[stub] eval: would run arms="
        f"{args.arms} on n={args.n} cases with seed={args.seed} "
        f"(record={args.record}, live={args.live}). Implemented in phases 3 and 5."
    )
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    target = args.run_id or "latest run"
    print(f"[stub] report: would summarise {target}. Implemented in phase 6.")
    return 0


def _cmd_sweep(args: argparse.Namespace) -> int:
    print(
        f"[stub] sweep: would sweep param={args.param} over range={args.range}. "
        "Implemented in phase 6."
    )
    return 0


def _cmd_repro(args: argparse.Namespace) -> int:
    print("[stub] repro: would run the clean-clone reproduction gate. Implemented in phase 8.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="recovery", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_eval = sub.add_parser("eval", help="run the evaluation over one or more arms")
    p_eval.add_argument("--seed", type=int, default=42)
    p_eval.add_argument("--n", type=int, default=500)
    p_eval.add_argument("--arms", type=lambda s: s.split(","), default=["control"])
    p_eval.add_argument("--record", action="store_true")
    p_eval.add_argument("--live", action="store_true")
    p_eval.set_defaults(func=_cmd_eval)

    p_cohort = sub.add_parser("cohort", help="regenerate the synthetic cohort artifact")
    p_cohort.add_argument("--seed", type=int, default=42)
    p_cohort.add_argument("--n", type=int, default=500)
    p_cohort.add_argument("--out", default=None, help="output path (default data/cohort_seed<seed>.jsonl)")
    p_cohort.set_defaults(func=_cmd_cohort)

    p_report = sub.add_parser("report", help="summarise a completed run")
    p_report.add_argument("--run-id", default=None)
    p_report.set_defaults(func=_cmd_report)

    p_sweep = sub.add_parser("sweep", help="sensitivity sweep over a simulation parameter")
    p_sweep.add_argument("--param", type=str, required=True)
    p_sweep.add_argument("--range", type=lambda s: [float(x) for x in s.split(",")], required=True)
    p_sweep.set_defaults(func=_cmd_sweep)

    p_repro = sub.add_parser("repro", help="clean-clone reproduction gate")
    p_repro.set_defaults(func=_cmd_repro)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
