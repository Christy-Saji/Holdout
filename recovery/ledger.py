"""Append-only, hash-chained audit ledger.

One entry per policy evaluation, action and outcome. Two properties are deliberate.

**Every evaluation is logged, allow and deny.** The policy engine does not
short-circuit, so ``verdicts`` carries the full rule set's opinion on every action and
the denials become the blocked-action log. An audit trail that records only what
happened is half an audit trail; what the system *refused to do* is the interesting
half.

**The chain is the append-only guarantee.** Each entry's hash covers its own body plus
its predecessor's hash, so editing entry *k* invalidates entry *k*, and re-hashing
entry *k* to cover the edit invalidates entry *k+1*. Append-only becomes a structural
property rather than a claim. There is deliberately **no update path and no delete
path** in this module; do not add one.

``ts`` is the *simulated* clock passed in by the harness, never ``datetime.now()``.
Wall-clock timestamps would make runs non-reproducible and would fail the phase 8
clean-clone reproduction gate.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

GENESIS_PREV_HASH = "0" * 64

KIND_POLICY_CHECK = "policy_check"
KIND_ACTION = "action"
KIND_OUTCOME = "outcome"
KIND_OBSERVATION = "observation"
KINDS = frozenset({KIND_POLICY_CHECK, KIND_ACTION, KIND_OUTCOME, KIND_OBSERVATION})


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    prev_hash: str  # hash chain -- tamper-evidence
    entry_hash: str
    ts: datetime  # simulated clock, not wall clock
    run_id: str
    arm: str
    case_id: str
    kind: str  # policy_check | action | outcome | observation
    action: dict | None
    verdicts: list  # EVERY rule verdict, pass and fail
    allowed: bool | None
    idempotency_key: str | None
    cost_paise: int
    result: dict | None


def canonical_json(obj: Any) -> str:
    """Sorted keys, no incidental whitespace.

    Default ``json.dumps`` preserves insertion order and pads with spaces, so two
    logically identical entries would hash differently depending on how their dicts
    happened to be built. Canonicalising makes the hash stable across Python versions
    and dict construction order.
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _body(entry_fields: dict) -> dict:
    """The hashed body: every field except ``entry_hash``, with ``ts`` as ISO-8601."""
    body = {k: v for k, v in entry_fields.items() if k != "entry_hash"}
    ts = body["ts"]
    body["ts"] = ts.isoformat() if isinstance(ts, datetime) else ts
    return body


def compute_entry_hash(entry_fields: dict, prev_hash: str) -> str:
    """``sha256(canonical_json(entry_without_entry_hash) + prev_hash)``."""
    payload = canonical_json(_body(entry_fields)) + prev_hash
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def entry_to_dict(entry: LedgerEntry) -> dict:
    fields = {
        "seq": entry.seq,
        "prev_hash": entry.prev_hash,
        "entry_hash": entry.entry_hash,
        "ts": entry.ts,
        "run_id": entry.run_id,
        "arm": entry.arm,
        "case_id": entry.case_id,
        "kind": entry.kind,
        "action": entry.action,
        "verdicts": entry.verdicts,
        "allowed": entry.allowed,
        "idempotency_key": entry.idempotency_key,
        "cost_paise": entry.cost_paise,
        "result": entry.result,
    }
    out = _body(fields)
    out["entry_hash"] = entry.entry_hash
    return out


class Ledger:
    """Append-only JSONL writer. No update path, no delete path.

    Re-opening an existing ledger resumes the chain from its last entry, so a run
    split across processes still produces one verifiable chain.
    """

    def __init__(self, path: Path | str, run_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self._seq = 0
        self._prev_hash = GENESIS_PREV_HASH
        if self.path.exists():
            for row in read_entries(self.path):
                self._seq = row["seq"] + 1
                self._prev_hash = row["entry_hash"]

        self._fh = self.path.open("a", encoding="utf-8", newline="\n")

    # -- writing -----------------------------------------------------------------

    def append(
        self,
        *,
        ts: datetime,
        arm: str,
        case_id: str,
        kind: str,
        action: dict | None = None,
        verdicts: list | None = None,
        allowed: bool | None = None,
        idempotency_key: str | None = None,
        cost_paise: int = 0,
        result: dict | None = None,
    ) -> LedgerEntry:
        """Append one entry and flush. ``ts`` must be the simulated clock."""
        if kind not in KINDS:
            raise ValueError(f"unknown ledger kind {kind!r}; expected one of {sorted(KINDS)}")
        if not isinstance(cost_paise, int) or isinstance(cost_paise, bool):
            raise TypeError(f"cost_paise must be an int (paise), got {type(cost_paise).__name__}")

        fields = {
            "seq": self._seq,
            "prev_hash": self._prev_hash,
            "ts": ts,
            "run_id": self.run_id,
            "arm": arm,
            "case_id": case_id,
            "kind": kind,
            "action": action,
            "verdicts": list(verdicts or []),
            "allowed": allowed,
            "idempotency_key": idempotency_key,
            "cost_paise": cost_paise,
            "result": result,
        }
        entry_hash = compute_entry_hash(fields, self._prev_hash)
        entry = LedgerEntry(entry_hash=entry_hash, **fields)  # type: ignore[arg-type]

        self._fh.write(canonical_json(entry_to_dict(entry)) + "\n")
        self._fh.flush()

        self._seq += 1
        self._prev_hash = entry_hash
        return entry

    # -- lifecycle ---------------------------------------------------------------

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "Ledger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @property
    def seq(self) -> int:
        """The sequence number the next appended entry will carry."""
        return self._seq

    @property
    def head_hash(self) -> str:
        return self._prev_hash


def read_entries(path: Path | str) -> Iterator[dict]:
    """Yield raw ledger rows in file order."""
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def verify(path: Path | str) -> bool:
    """Walk the file and recompute the chain. ``True`` only if every link holds."""
    expected_seq = 0
    prev_hash = GENESIS_PREV_HASH

    for row in read_entries(path):
        if row.get("seq") != expected_seq:
            return False
        if row.get("prev_hash") != prev_hash:
            return False

        stated = row.get("entry_hash")
        if compute_entry_hash(row, prev_hash) != stated:
            return False

        prev_hash = stated
        expected_seq += 1

    return True
