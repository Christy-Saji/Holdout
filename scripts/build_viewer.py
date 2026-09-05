"""Generate ``docs/index.html`` from the committed run artifacts.

**The viewer is generated, never hand-edited.** Every figure on the page is derived
here from the same ledgers the command line reads, so the page cannot drift from the
numbers ``recovery.cli report`` prints. A hand-maintained dashboard that quietly
disagreed with the repository's own results would be worse than no dashboard.

The output is a single self-contained HTML file with its data inlined -- no server, no
build step, no network. Open it with a double click, or serve ``docs/`` with GitHub
Pages.

This module is **not** part of the measurement pipeline. Nothing in ``recovery/``
imports it, and deleting it changes no published number.

    python scripts/build_viewer.py
"""

from __future__ import annotations

import collections
import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from recovery import cohort as cohort_mod  # noqa: E402
from recovery.cohort import u  # noqa: E402
from recovery.eval import harness, metrics  # noqa: E402
from recovery.ledger import KIND_POLICY_CHECK, read_entries  # noqa: E402

RUN_DIR = ROOT / "data" / "results" / "run-s42-n500"
SWEEP_ROOT = ROOT / "data" / "results" / "sweeps"
SWEEP_JSON = SWEEP_ROOT / "p_self_heal" / "sweep-p_self_heal.json"
TEMPLATE = ROOT / "scripts" / "viewer.template.html"
FONTS_CSS = ROOT / "scripts" / "fonts" / "fonts.css"
OUT = ROOT / "docs" / "index.html"

CHAIN_PREVIEW_ENTRIES = 3

# Order the sensitivity selector offers. p_self_heal leads because it is the parameter
# the whole critique rests on; the others are there so the page cannot be accused of
# sweeping only the one that flatters it.
SWEEP_PARAMS = ("p_self_heal", "attempt_decay", "retry_cost")


def bootstrap_replicates(arm, control, seed: int, replicates: int) -> list[int]:
    """The individual replicate totals, so the page can draw the distribution.

    Deliberately re-derived with the same keying as
    :func:`recovery.eval.metrics.bootstrap_incremental` rather than reimplemented with
    a different one -- if these two ever disagree, the histogram would be describing an
    interval nobody published.
    """
    pairs = metrics._paired(arm, control)
    n = len(pairs)
    deltas = [a.recovered_paise - c.recovered_paise for a, c in pairs]
    return [
        sum(deltas[int(u(seed, f"boot-{b}", "bootstrap_index", i) * n)] for i in range(n))
        for b in range(replicates)
    ]


def recovery_by_day(outcomes, window_h: int) -> list[int]:
    days = max(1, window_h // 24)
    counts = collections.Counter(
        o.recovered_at_hour // 24
        for o in outcomes
        if o.recovered and o.recovered_at_hour is not None
    )
    return [counts.get(d, 0) for d in range(days)]


def recovery_causes(outcomes) -> dict[str, int]:
    """How each recovery came about. The retry/self-heal split is the whole argument."""
    return dict(collections.Counter(o.recovery_cause for o in outcomes if o.recovered))


def first_denial(ledger_path: Path) -> dict | None:
    for row in read_entries(ledger_path):
        if row["kind"] == KIND_POLICY_CHECK and row.get("allowed") is False:
            return row
    return None


def compact_timeline(ledger_path: Path) -> dict[str, list[dict]]:
    """Every ledger entry for every case, stripped to what the replay actually draws.

    The committed baseline ledger is 4 MB, almost all of it the *allowed* verdicts: all
    twelve rules record a verdict on every action, which is the point of the
    no-short-circuit design but is not something a browser needs a copy of. So an
    allowed action keeps only its shape, and the full rule text is retained exactly
    where it earns its place -- on the refusals. That is the half a reviewer came to
    read, and it is the half the ledger exists to make undeniable.
    """
    by_case: dict[str, list[dict]] = collections.defaultdict(list)
    for row in read_entries(ledger_path):
        action = row.get("action") or {}
        denied = [
            {"rule": v["rule_id"], "why": v["reason"]}
            for v in (row.get("verdicts") or [])
            if not v["allow"]
        ]

        # An allowed policy_check is always followed by the action it authorised, and
        # the two would render as the same row. Only the refusals survive as checks --
        # which is also the half worth reading.
        if row["kind"] == KIND_POLICY_CHECK and not denied:
            continue

        entry = {"kind": row["kind"]}
        if action.get("at_hour") is not None:
            entry["hour"] = action["at_hour"]
        if action.get("type"):
            entry["type"] = action["type"]
        if action.get("channel"):
            entry["channel"] = action["channel"]
        if row.get("cost_paise"):
            entry["cost"] = row["cost_paise"]
        if denied:
            entry["denied"] = denied
        if row["kind"] == "outcome" and row.get("result"):
            entry["result"] = row["result"]
        by_case[row["case_id"]].append(entry)
    return dict(by_case)


def case_index(cohort_path: Path, run) -> list[dict]:
    """One compact row per case, joining the observable cohort fields to both arms.

    **Latents are not read here.** ``load_cohort`` is called without them, exactly as
    every arm calls it, so the page can only ever show what the system itself could
    see. Publishing the hidden self-heal propensity next to the outcome would make the
    whole holdout argument circular.
    """
    cases = cohort_mod.load_cohort(cohort_path)
    by_arm = {arm.arm: {o.case_id: o for o in arm.outcomes} for arm in run.arms}

    rows = []
    for case in cases:
        row = {
            "id": case.case_id,
            "amount": case.amount_paise,
            "method": case.method.value if hasattr(case.method, "value") else str(case.method),
            "reason": case.decline_reason,
            "recurring": case.is_recurring,
            "first_debit": case.mandate_first_debit,
            "category": case.mandate_category,
            "arms": {},
        }
        for arm, outcomes in by_arm.items():
            o = outcomes.get(case.case_id)
            if o is None:
                continue
            row["arms"][arm] = {
                "recovered": o.recovered,
                "at": o.recovered_at_hour,
                "cause": o.recovery_cause,
                "cost": o.cost_paise,
                "attempts": o.attempts,
                "contacts": o.contacts,
            }
        rows.append(row)
    return rows


def load_sweeps() -> dict[str, dict]:
    """Every committed sweep, keyed by parameter, in the order the selector offers."""
    found = {}
    for param in SWEEP_PARAMS:
        path = SWEEP_ROOT / param / f"sweep-{param}.json"
        if path.exists():
            found[param] = json.loads(path.read_text(encoding="utf-8"))
    return found


def collect(run_dir: Path) -> dict:
    run = harness.load_run(run_dir)
    computed = metrics.compute(run)
    intervals = metrics.confidence_intervals(run)

    by_arm = run.by_arm
    control = by_arm["control"].outcomes
    treatment_name = next((a.arm for a in run.arms if a.arm != "control"), "control")
    treatment = by_arm[treatment_name].outcomes

    payload = {
        "run": {
            "run_id": run.run_id,
            "seed": run.seed,
            "n": run.n,
            "window_h": run.params.window_h,
        },
        "arms": [
            {
                "arm": m.arm,
                "n": m.n,
                "n_recovered": m.n_recovered,
                "gross_paise": m.gross_paise,
                "incremental_paise": m.incremental_paise,
                "recovery_rate": m.recovery_rate,
                "cost_paise": m.cost_paise,
                "cpi": m.cost_per_incremental_rupee,
                "blocked": m.blocked_actions,
                "blocked_by_rule": m.blocked_by_rule,
                "attempts": m.attempts,
                "contacts": m.contacts,
                "time_to_cash_days": m.time_to_cash_days,
                "customers": m.customers,
                "contacts_mean": m.contacts_mean,
                "contacts_p95": m.contacts_p95,
            }
            for m in computed
        ],
        "ci": {name: ci.to_dict() for name, ci in intervals.items()},
        "bootstrap_replicates": bootstrap_replicates(
            treatment, control, run.seed, metrics.BOOTSTRAP_REPLICATES
        ),
        "timing": {
            arm.arm: recovery_by_day(arm.outcomes, run.params.window_h) for arm in run.arms
        },
        "causes": {arm.arm: recovery_causes(arm.outcomes) for arm in run.arms},
        "denial": first_denial(run_dir / f"{treatment_name}.jsonl"),
        "chain": list(
            itertools.islice(
                read_entries(run_dir / f"{treatment_name}.jsonl"), CHAIN_PREVIEW_ENTRIES
            )
        ),
        "costs": run.params.to_dict()["costs"],
        "cases": case_index(cohort_mod.cohort_path_for(run.seed), run),
        "timelines": {
            arm.arm: compact_timeline(run_dir / f"{arm.arm}.jsonl") for arm in run.arms
        },
        "sweeps": load_sweeps(),
        # The blob is emitted with sorted keys so the committed page is byte-stable,
        # which would otherwise alphabetise the selector and open it on the wrong
        # parameter. The intended order therefore travels as its own list.
        "sweep_order": [p for p in SWEEP_PARAMS if (SWEEP_ROOT / p).exists()],
    }

    if SWEEP_JSON.exists():
        payload["sweep"] = json.loads(SWEEP_JSON.read_text(encoding="utf-8"))
    else:
        payload["sweep"] = {"points": [], "sign_survives": True}
    return payload


def main() -> int:
    if not (RUN_DIR / "manifest.json").exists():
        print(f"no run at {RUN_DIR}. Run `python -m recovery.cli eval` first.")
        return 1
    if not SWEEP_JSON.exists():
        print(
            f"note: no sweep summary at {SWEEP_JSON}; the sensitivity section will be "
            "empty. Run `python -m recovery.cli sweep --param p_self_heal "
            "--range 0.5,1.5` to populate it."
        )

    payload = collect(RUN_DIR)
    # Separators without spaces keep the inlined blob small; sort_keys keeps the
    # generated file byte-stable across runs, which matters because it is committed.
    blob = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    # `</script>` inside a script element would close it early. The escape is invisible
    # to JSON.parse and cannot appear in any value we emit, but costs nothing to make safe.
    blob = blob.replace("</", "<\\/")

    # The fonts are inlined from a committed file rather than fetched, so this build
    # stays offline and byte-stable. See scripts/fetch_fonts.py for where they came from.
    fonts = FONTS_CSS.read_text(encoding="utf-8") if FONTS_CSS.exists() else ""
    if not fonts:
        print(
            f"note: no {FONTS_CSS.name}; the page will fall back to system fonts. "
            "Run `python scripts/fetch_fonts.py` once to embed them."
        )

    html = (
        TEMPLATE.read_text(encoding="utf-8")
        .replace("/*__FONTS__*/", fonts)
        .replace("__DATA__", blob)
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(html, encoding="utf-8", newline="\n")

    # relative_to raises when OUT has been redirected outside the repo, which the
    # viewer tests do so that a test run never rewrites the committed artifact.
    try:
        shown = OUT.relative_to(ROOT)
    except ValueError:
        shown = OUT
    print(f"wrote {shown}  ({OUT.stat().st_size:,} bytes, data {len(blob):,})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
