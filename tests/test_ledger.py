"""Chain verification and tamper detection for the audit ledger.

These tests are what turn "append-only" from a claim into a property. Each one
corresponds to a specific way someone could edit the file after the fact.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from recovery.ledger import (
    GENESIS_PREV_HASH,
    KIND_ACTION,
    KIND_POLICY_CHECK,
    Ledger,
    compute_entry_hash,
    read_entries,
    verify,
)

IST = timezone(timedelta(hours=5, minutes=30), name="IST")
CLOCK = datetime(2026, 8, 1, 10, 0, tzinfo=IST)  # simulated, never datetime.now()


def _write_chain(path, n: int = 5) -> None:
    """A small, realistic chain: policy checks (some denied) followed by actions."""
    with Ledger(path, run_id="run-test") as ledger:
        for i in range(n):
            allowed = i % 2 == 0
            ledger.append(
                ts=CLOCK + timedelta(hours=i),
                arm="agent",
                case_id=f"case-{i:05d}",
                kind=KIND_POLICY_CHECK,
                action={"type": "retry", "attempt": 1},
                verdicts=[
                    {"rule_id": "HardDeclineNoRetry", "allow": True, "reason": "soft decline"},
                    {"rule_id": "CoolingOff", "allow": allowed, "reason": "min_retry_wait_h"},
                ],
                allowed=allowed,
                idempotency_key=f"case-{i:05d}:retry:1",
                cost_paise=200 if allowed else 0,
            )


def test_a_freshly_written_chain_verifies(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _write_chain(path)
    assert verify(path) is True


def test_an_empty_ledger_verifies(tmp_path):
    path = tmp_path / "ledger.jsonl"
    Ledger(path, run_id="run-test").close()
    assert verify(path) is True


def test_genesis_prev_hash_is_sixty_four_zeros(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _write_chain(path, n=3)
    first = next(iter(read_entries(path)))
    assert first["prev_hash"] == GENESIS_PREV_HASH
    assert first["prev_hash"] == "0" * 64
    assert first["seq"] == 0


@pytest.mark.parametrize(
    "field,new_value",
    [
        ("cost_paise", 999999),
        ("allowed", False),
        ("case_id", "case-99999"),
        ("arm", "control"),
        ("kind", KIND_ACTION),
        ("ts", "2026-12-25T00:00:00+05:30"),
        ("run_id", "run-other"),
        ("idempotency_key", "tampered"),
        ("verdicts", []),
        ("action", {"type": "contact"}),
        ("result", {"recovered": True}),
        ("seq", 42),
        ("prev_hash", "f" * 64),
    ],
)
def test_mutating_any_field_breaks_verification(tmp_path, field, new_value):
    path = tmp_path / "ledger.jsonl"
    _write_chain(path)

    rows = list(read_entries(path))
    rows[2][field] = new_value
    path.write_text(
        "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
        newline="\n",
    )

    assert verify(path) is False


def test_rehashing_a_tampered_entry_still_fails_via_the_next_link(tmp_path):
    """The interesting attack: edit the body *and* recompute that entry's own
    entry_hash so it is internally consistent. The next entry's prev_hash still points
    at the old hash, so the chain breaks one link later."""
    path = tmp_path / "ledger.jsonl"
    _write_chain(path)

    rows = list(read_entries(path))
    victim = rows[2]
    victim["cost_paise"] = 0
    victim["allowed"] = False
    victim["entry_hash"] = compute_entry_hash(victim, victim["prev_hash"])

    # The forged entry hashes correctly against its own body...
    assert compute_entry_hash(victim, victim["prev_hash"]) == victim["entry_hash"]

    path.write_text(
        "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
        newline="\n",
    )

    # ...but rows[3].prev_hash still names the entry that used to be there.
    assert verify(path) is False


def test_deleting_an_entry_breaks_verification(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _write_chain(path)

    rows = list(read_entries(path))
    del rows[2]
    path.write_text(
        "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
        newline="\n",
    )

    assert verify(path) is False


def test_reordering_entries_breaks_verification(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _write_chain(path)

    rows = list(read_entries(path))
    rows[1], rows[2] = rows[2], rows[1]
    path.write_text(
        "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows),
        encoding="utf-8",
        newline="\n",
    )

    assert verify(path) is False


def test_denials_are_recorded_not_dropped(tmp_path):
    """The blocked-action log. An audit trail that records only what happened is half
    an audit trail."""
    path = tmp_path / "ledger.jsonl"
    _write_chain(path, n=6)

    rows = list(read_entries(path))
    assert any(r["allowed"] is False for r in rows)
    assert any(r["allowed"] is True for r in rows)
    # Every verdict is present on every entry, passing and failing alike.
    assert all(len(r["verdicts"]) == 2 for r in rows)


def test_reopening_a_ledger_resumes_the_chain(tmp_path):
    path = tmp_path / "ledger.jsonl"
    _write_chain(path, n=3)

    with Ledger(path, run_id="run-test") as ledger:
        assert ledger.seq == 3
        entry = ledger.append(
            ts=CLOCK + timedelta(hours=9),
            arm="agent",
            case_id="case-00009",
            kind=KIND_ACTION,
            result={"status": "recovered"},
            cost_paise=0,
        )

    assert entry.seq == 3
    assert verify(path) is True
    assert len(list(read_entries(path))) == 4


def test_timestamps_are_the_simulated_clock(tmp_path):
    """Wall-clock timestamps would make runs non-reproducible and fail the phase 8
    reproduction gate, so the ledger records exactly what the harness handed it."""
    path = tmp_path / "ledger.jsonl"
    _write_chain(path, n=2)
    rows = list(read_entries(path))
    assert rows[0]["ts"] == CLOCK.isoformat()
    assert rows[1]["ts"] == (CLOCK + timedelta(hours=1)).isoformat()


def test_ledger_exposes_no_update_or_delete_path():
    """Structural, not aspirational: if someone adds one, this test tells them why
    they should not have."""
    forbidden = {"update", "delete", "remove", "edit", "truncate", "rewrite", "pop"}
    assert not forbidden & set(dir(Ledger))


def test_unknown_kind_is_rejected(tmp_path):
    path = tmp_path / "ledger.jsonl"
    with Ledger(path, run_id="run-test") as ledger:
        with pytest.raises(ValueError):
            ledger.append(ts=CLOCK, arm="agent", case_id="case-1", kind="whatever")


def test_float_money_is_rejected(tmp_path):
    """All money is integer paise. A float cost in the ledger is a correctness bug."""
    path = tmp_path / "ledger.jsonl"
    with Ledger(path, run_id="run-test") as ledger:
        with pytest.raises(TypeError):
            ledger.append(
                ts=CLOCK,
                arm="agent",
                case_id="case-1",
                kind=KIND_ACTION,
                cost_paise=2.5,  # type: ignore[arg-type]
            )
