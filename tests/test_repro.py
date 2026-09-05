"""The clean-clone reproduction gate.

The gate exists to back one sentence in the README: *clone this, run one command, get
the published number.* These tests exist because a gate that passes on a partial match
is worse than no gate -- it converts an unnoticed drift into a signed certificate that
there is none.

The first test is a regression on ``WHAT_BROKE.md`` incident 2: a truncated cohort is
a strict *prefix* of the real one, so it is byte-identical as far as it goes, and only
a separate length check against the manifest catches it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recovery import cohort as cohort_mod
from recovery.eval import harness, repro

SEED = 42
N = 20


@pytest.fixture(scope="module")
def published(tmp_path_factory) -> tuple[Path, Path]:
    """A small committed run: a cohort artifact and a results directory beside it."""
    root = tmp_path_factory.mktemp("published")
    cases = cohort_mod.generate_cohort(seed=SEED, n=N)
    cohort_path = cohort_mod.write_cohort(cases, root / f"cohort_seed{SEED}.jsonl")
    harness.run(
        seed=SEED,
        n=N,
        arms=["control", "baseline"],
        cases=cases,
        out_root=root / "results",
        run_id=f"run-s{SEED}-n{N}",
    )
    return cohort_path, root / "results"


def run_gate(published, capsys) -> tuple[int, str]:
    cohort_path, results_root = published
    code = repro.main(results_root=results_root, cohort_path=cohort_path)
    return code, capsys.readouterr().out


def test_untouched_artifacts_reproduce(published, capsys):
    code, out = run_gate(published, capsys)
    assert code == 0
    assert "all 6 checks passed" in out
    assert "FAIL" not in out


def test_a_truncated_cohort_is_caught_even_though_it_is_a_valid_prefix(published, capsys, tmp_path):
    """WHAT_BROKE.md incident 2.

    Every line of the truncated file is a real, correctly generated case. Only the
    count is wrong, so only a count check against the manifest can see it.
    """
    cohort_path, results_root = published
    original = cohort_path.read_text(encoding="utf-8")
    truncated = "\n".join(original.strip().splitlines()[:5]) + "\n"
    cohort_path.write_text(truncated, encoding="utf-8", newline="\n")
    try:
        code = repro.main(results_root=results_root, cohort_path=cohort_path)
        out = capsys.readouterr().out
    finally:
        cohort_path.write_text(original, encoding="utf-8", newline="\n")

    assert code == 1
    assert "REPRODUCTION FAILED" in out
    count_row = next(line for line in out.splitlines() if "case count" in line)
    assert count_row.strip().startswith("[FAIL]")
    assert "5 lines, manifest says 20" in count_row


def test_a_tampered_ledger_line_fails_the_byte_comparison(published, capsys):
    cohort_path, results_root = published
    ledger = results_root / f"run-s{SEED}-n{N}" / "baseline.jsonl"
    original = ledger.read_bytes()
    rows = original.decode("utf-8").strip().splitlines()
    doctored = json.loads(rows[-1])
    doctored["cost_paise"] = doctored.get("cost_paise", 0) + 1
    rows[-1] = json.dumps(doctored, sort_keys=True, separators=(",", ":"))
    ledger.write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    try:
        code = repro.main(results_root=results_root, cohort_path=cohort_path)
        out = capsys.readouterr().out
    finally:
        ledger.write_bytes(original)

    assert code == 1
    assert "baseline ledger reproduces" in out
    assert "REPRODUCTION FAILED" in out


def test_a_missing_cohort_file_fails_rather_than_raising(published, capsys, tmp_path):
    _, results_root = published
    code = repro.main(results_root=results_root, cohort_path=tmp_path / "absent.jsonl")
    out = capsys.readouterr().out
    assert code == 1
    assert "is missing" in out


def test_the_gate_reuses_the_run_id_so_the_ledgers_are_comparable(published, tmp_path):
    """A re-run under a fresh run id would differ on every line and prove nothing."""
    _, results_root = published
    run_dir = results_root / f"run-s{SEED}-n{N}"
    checks = repro.check_run(run_dir, tmp_path)
    assert all(c.passed for c in checks), [c.detail for c in checks if not c.passed]
    regenerated = tmp_path / f"run-s{SEED}-n{N}" / "control.jsonl"
    assert regenerated.exists()


# -- the clean-clone gate -------------------------------------------------------------


def test_credentials_are_stripped_from_the_child_environment(monkeypatch):
    """The gate must prove the numbers need no credentials, including on a machine that
    has them. Leaving the keys merely unset would prove that only on a machine without."""
    seen = {}

    def fake_run(cmd, cwd=None, env=None, **kwargs):
        seen.update(env or {})

        class P:
            returncode = 0
            stdout = ""
            stderr = ""

        return P()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-survive")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "should-not-survive")
    monkeypatch.setattr(repro.subprocess, "run", fake_run)

    repro._sh(["true"], Path.cwd())

    for blocked in repro.BLOCKED_ENV:
        assert blocked not in seen, f"{blocked} reached the child process"


def test_the_worktree_source_copies_the_files_git_would_include(tmp_path, monkeypatch):
    """``--worktree`` exists to check a reproduction before the work is committed."""
    calls = []

    def fake_sh(cmd, cwd):
        calls.append(cmd)
        return 0, "recovery/cli.py\npyproject.toml\n"

    monkeypatch.setattr(repro, "_sh", fake_sh)
    ok, detail = repro._materialise(tmp_path / "dest", worktree=True)

    assert ok
    assert "--exclude-standard" in calls[0], "ignored files must not be copied in"
    assert "2" in detail


def test_the_default_source_clones_head_rather_than_the_working_tree(tmp_path, monkeypatch):
    """The honest question is whether a *clone* reproduces, so HEAD is the default."""
    monkeypatch.setattr(repro, "_sh", lambda cmd, cwd: (0, "") if "clone" in cmd else (1, ""))
    ok, detail = repro._materialise(tmp_path / "dest", worktree=False)

    assert ok and detail == "cloned HEAD"


def test_the_cli_exposes_both_repro_modes():
    from recovery.cli import build_parser

    args = build_parser().parse_args(["repro"])
    assert args.in_process is False and args.worktree is False

    args = build_parser().parse_args(["repro", "--in-process"])
    assert args.in_process is True
