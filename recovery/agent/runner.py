"""The agentic loop, and the cassette layer that makes it reproducible offline.

**The loop is the SDK's, not ours.** ``client.beta.messages.tool_runner`` drives the
turn cycle over the tools in :mod:`recovery.agent.tools`; there is no
``while stop_reason == "tool_use"`` anywhere in this project. Hand-rolling it would
re-implement, worse, the approval gating and result interception the runner already
provides -- and the approval gating is not needed at that level anyway, because it
lives *inside* each tool function where the model can see the refusal and react to it.

**Cassettes are what let a reviewer reproduce the published number with no API key.**
Every LLM turn is recorded, keyed by a canonical hash of the whole request, and replay
is the default. Clone the repo, run the eval, get the same number, offline.

The interception point is ``client.beta.messages.parse`` -- the single method the
runner calls per turn (``anthropic/lib/tools/_beta_runner.py``). Replacing that one
bound method on the client instance leaves the runner's loop untouched and means there
is exactly one seam to reason about.

Because the cassette key covers the *entire* request, including the tool results from
every previous turn, the format self-validates: if any tool returns something different
on replay than it did on the recording run, the next turn's key does not match and the
run **raises**. Replay cannot silently drift into a different conversation.

**A miss in replay mode raises. It never falls through to a live call.** A reviewer
without credentials must get a loud failure rather than a quiet, expensive,
non-reproducible one -- and a run that silently went live would no longer be the run
the README reports.

There is no ``datetime.now()`` in this module. A wall clock in the request would change
the cassette key on every run and invalidate the prompt cache prefix at the same time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from recovery.agent import prompts, tools
from recovery.agent.tools import Session
from recovery.policy.engine import Gate, rules_summary
from recovery.transport.base import Action, CaseState, SimParams

DEFAULT_CASSETTE_ROOT = Path("data/cassettes")

MODE_REPLAY = "replay"
MODE_RECORD = "record"
MODE_LIVE = "live"
MODES = (MODE_REPLAY, MODE_RECORD, MODE_LIVE)

MAX_ITERATIONS = 12
"""Turns per case. A plan is a handful of tool calls; anything approaching this is a
model that has lost the thread, and the bound is what stops one case from eating the
budget for the other four hundred and ninety nine."""

REPLAY_API_KEY = "cassette-replay-no-network"
"""The SDK requires *a* credential to construct a client. In replay mode nothing ever
reaches the network -- a miss raises before any request is built -- so this placeholder
is what lets the whole eval run with ``ANTHROPIC_API_KEY`` unset."""

KEYED_REQUEST_FIELDS = (
    "max_tokens",
    "messages",
    "model",
    "output_config",
    "system",
    "thinking",
    "tool_choice",
    "tools",
)
"""What the cassette key is computed over. Everything that can change the model's
answer, and nothing that cannot -- request ids, timeouts and headers are excluded
because varying them must not invalidate a cassette."""


class CassetteMiss(RuntimeError):
    """Replay was asked for a turn that was never recorded."""


# -- canonical request hashing --------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Everything the SDK may hand us, reduced to plain JSON types.

    Request params arrive as a mix of dicts, pydantic models (assistant messages the
    runner appended) and SDK sentinels for omitted arguments. All three have to
    canonicalise identically across a record run and a replay run.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items() if not _is_omitted(v)}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value if not _is_omitted(v)]
    for attr in ("model_dump", "to_dict"):
        dump = getattr(value, attr, None)
        if callable(dump):
            try:
                return _jsonable(dump(mode="json") if attr == "model_dump" else dump())
            except TypeError:
                return _jsonable(dump())
    return str(value)


def _is_omitted(value: Any) -> bool:
    """SDK sentinels for "argument not given", which must not reach the key."""
    return value is None or type(value).__name__ in {"Omit", "NotGiven"}


def canonical_request(params: dict) -> dict:
    """The part of a request that decides the answer, in a stable shape."""
    return {
        field_name: _jsonable(params[field_name])
        for field_name in KEYED_REQUEST_FIELDS
        if field_name in params and not _is_omitted(params[field_name])
    }


def request_key(params: dict) -> str:
    """``sha256`` over the canonical request, with **sorted keys**.

    Unsorted JSON would mean a logically identical request hashing differently between
    two runs -- a miss on every turn, and in a design that fell through to the network
    that would silently turn a reproduction into a live, unreproducible, billed run.
    """
    blob = json.dumps(canonical_request(params), sort_keys=True, separators=(",", ":"))
    return sha256(blob.encode("utf-8")).hexdigest()


# -- the cassette store ---------------------------------------------------------------


@dataclass
class CassetteStore:
    """Recorded LLM turns on disk, one JSON file per case.

    Per case rather than per turn because a 500-case run is some two thousand turns,
    and two thousand files is a directory nobody can read. Per case, a reviewer can
    open one file and see the whole conversation the agent had about one payment.
    """

    root: Path = DEFAULT_CASSETTE_ROOT
    mode: str = MODE_REPLAY
    current_case: str = ""
    """The case being planned right now.

    The client is built once per run and reused across all 500 cases -- a client per
    case would mean a connection pool per case on a recording run -- so the case a turn
    belongs to has to reach the interceptor some other way. The planner sets this
    before each case.
    """

    _loaded: dict[str, dict[str, dict]] = field(default_factory=dict, init=False)
    _recorded: dict[str, list[dict]] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"unknown cassette mode {self.mode!r}; expected one of {MODES}")
        self.root = Path(self.root)

    def path_for(self, case_id: str) -> Path:
        return self.root / f"{case_id}.json"

    def _turns(self, case_id: str) -> dict[str, dict]:
        if case_id not in self._loaded:
            path = self.path_for(case_id)
            if path.exists():
                payload = json.loads(path.read_text(encoding="utf-8"))
                self._loaded[case_id] = {t["key"]: t["response"] for t in payload["turns"]}
            else:
                self._loaded[case_id] = {}
        return self._loaded[case_id]

    def fetch(self, case_id: str, key: str) -> dict:
        """The recorded response for this turn, or raise. Never a live fallthrough."""
        turns = self._turns(case_id)
        try:
            return turns[key]
        except KeyError:
            raise CassetteMiss(
                f"no cassette for case {case_id} turn {key[:12]} in {self.path_for(case_id)}. "
                "The request differs from what was recorded -- a changed prompt, a changed "
                "tool schema or a changed tool result. Re-record with --record; replay will "
                "not fall through to a live API call."
            ) from None

    def put(self, case_id: str, key: str, response: dict) -> None:
        self._recorded.setdefault(case_id, []).append({"key": key, "response": response})

    def flush(self, case_id: str) -> None:
        """Write one case's recorded turns. Sorted keys, so a re-record diffs cleanly."""
        turns = self._recorded.pop(case_id, None)
        if not turns:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"case_id": case_id, "turns": turns}
        self.path_for(case_id).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )


# -- the client -------------------------------------------------------------------------


def build_client(store: CassetteStore):
    """An Anthropic client whose one API call goes through the cassette store.

    ``client.beta.messages`` is a cached property, so the object patched here is the
    same one ``BetaToolRunner._handle_request`` reaches for. The runner is otherwise
    untouched: it still owns the turn loop, the tool dispatch and the message history.

    In replay mode the client is constructed with a placeholder credential and never
    reaches the network -- a miss raises before a request is built -- which is what
    lets the published number reproduce with ``ANTHROPIC_API_KEY`` unset.
    """
    import anthropic

    client = (
        anthropic.Anthropic()
        if store.mode in (MODE_RECORD, MODE_LIVE)
        else anthropic.Anthropic(api_key=REPLAY_API_KEY)
    )
    messages = client.beta.messages
    live_parse = messages.parse

    def parse(**params):
        key = request_key(params)
        case_id = store.current_case
        if store.mode == MODE_REPLAY:
            from anthropic.types.beta.parsed_beta_message import ParsedBetaMessage

            return ParsedBetaMessage.model_validate(store.fetch(case_id, key))
        message = live_parse(**params)
        if store.mode == MODE_RECORD:
            store.put(case_id, key, message.model_dump(mode="json"))
        return message

    messages.parse = parse  # type: ignore[method-assign]
    return client


# -- planning one case --------------------------------------------------------------------


@dataclass(frozen=True)
class PlanResult:
    """One case's plan, plus what it cost to produce."""

    case_id: str
    actions: tuple[Action, ...]
    closed: bool
    close_reason: str | None
    denials: int
    turns: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int

    def to_dict(self) -> dict:
        return {
            "case_id": self.case_id,
            "actions": [a.to_dict() for a in self.actions],
            "closed": self.closed,
            "close_reason": self.close_reason,
            "denials": self.denials,
            "turns": self.turns,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
        }


@dataclass
class Planner:
    """Turns one :class:`CaseState` into a plan of scheduled, policy-screened actions."""

    gate: Gate
    params: SimParams
    store: CassetteStore
    system: str = field(init=False)
    _client: object | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        # Rendered once per run from the engine itself, so the rule summaries the model
        # reads cannot drift from the rules that actually gate it -- and so the cached
        # prefix is byte-identical for every case.
        self.system = prompts.system_prompt(list(rules_summary(self.gate.engine)), self.params)

    @property
    def client(self):
        """One client for the whole run, so the connection pool is shared."""
        if self._client is None:
            self._client = build_client(self.store)
        return self._client

    def plan(self, state: CaseState) -> PlanResult:
        session = Session(gate=self.gate, state=state, params=self.params)
        self.store.current_case = state.case_id
        token = tools.bind(session)
        try:
            stats = self._run(state, session)
        finally:
            tools.unbind(token)
            if self.store.mode == MODE_RECORD:
                self.store.flush(state.case_id)

        return PlanResult(
            case_id=state.case_id,
            actions=session.actions,
            closed=session.closed,
            close_reason=session.close_reason,
            denials=session.denials,
            **stats,
        )

    def _run(self, state: CaseState, session: Session) -> dict:
        runner = self.client.beta.messages.tool_runner(
            model=prompts.MODEL,
            max_tokens=prompts.MAX_TOKENS,
            thinking=prompts.THINKING,
            output_config=prompts.OUTPUT_CONFIG,
            max_iterations=MAX_ITERATIONS,
            # A list of blocks rather than a bare string, so the cache breakpoint sits
            # at the end of the stable prefix. Everything that varies per case is in
            # the user message, after it.
            system=[
                {
                    "type": "text",
                    "text": self.system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=list(tools.TOOLS),
            messages=[{"role": "user", "content": prompts.case_brief(state)}],
        )

        turns = input_tokens = output_tokens = cache_read = 0
        for message in runner:
            turns += 1
            # The Python runner does not auto-resume a paused turn: it exits and hands
            # back the paused message as final, with no error. This project uses no
            # server-side tools so it should not arise, and if it ever does, a
            # truncated plan silently becoming "the agent's decision" is exactly the
            # kind of quiet wrongness this submission is trying not to have.
            if message.stop_reason == "pause_turn":
                raise RuntimeError(
                    f"tool runner paused on case {state.case_id}; the plan would be "
                    "silently truncated"
                )
            usage = getattr(message, "usage", None)
            if usage is not None:
                input_tokens += getattr(usage, "input_tokens", 0) or 0
                output_tokens += getattr(usage, "output_tokens", 0) or 0
                cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0

        return {
            "turns": turns,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": cache_read,
        }


def summarise(results: Iterable[PlanResult]) -> dict:
    """Run-level agent statistics, for the CLI to print after a recording run."""
    results = list(results)
    return {
        "cases_planned": len(results),
        "cases_closed": sum(1 for r in results if r.closed),
        "actions_planned": sum(len(r.actions) for r in results),
        "plan_time_denials": sum(r.denials for r in results),
        "turns": sum(r.turns for r in results),
        "input_tokens": sum(r.input_tokens for r in results),
        "output_tokens": sum(r.output_tokens for r in results),
        "cache_read_tokens": sum(r.cache_read_tokens for r in results),
    }
