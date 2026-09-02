"""Command-line entry point for the recovery evaluation harness.

This phase wires the argument parser and leaves each handler as a stub that
reports what it would do and exits 0. Later phases fill the handlers in.
"""

from __future__ import annotations

import argparse


def _cmd_eval(args: argparse.Namespace) -> int:
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
