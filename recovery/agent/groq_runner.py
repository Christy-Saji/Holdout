"""The same gated agent, driven by a Groq-hosted open model instead of Claude.

**Why this file exists.** The agent arm was designed around
``client.beta.messages.tool_runner`` and never ran: recording its cassettes needs an
``ANTHROPIC_API_KEY`` that was not available before the submission date (``WHAT_BROKE.md``
incident 3). A free Groq key was. Rather than publish a third arm that had never
executed, the arm is re-driven here against an OpenAI-shaped chat-completions API.

**What this proves, and it is the point worth making.** ``recovery/agent/tools.py`` is
imported here **unchanged**. Every policy check still happens *inside* the tool
function, so the gate does not care which model is calling it -- the six tools, their
schemas and their refusals are shared byte for byte between the two runners. The
architecture put the approval seam in the one place that survives a provider swap, and
this file is the evidence rather than the claim.

**What is genuinely lost.** The Anthropic path gets its turn loop from the SDK; there is
no ``tool_runner`` on an OpenAI-compatible endpoint, so the ``while finish_reason ==
"tool_calls"`` loop that ``runner.py`` deliberately does not contain lives below. It is
about thirty lines, and writing it is the real cost of the swap. ``ARCHITECTURE.md``
says so plainly rather than quietly dropping the claim.

**What is shared.** :class:`~recovery.agent.runner.CassetteStore`, ``request_key`` and
the three modes are reused exactly. The key is a hash over the fields that decide the
answer, and the Anthropic-only ones (``system``, ``thinking``, ``output_config``) are
simply absent from a Groq request -- so the same hashing works for both without a
branch. Replay still raises on a miss and never falls through to a live call.

    python -m recovery.cli eval --arms agent --provider groq --record
    python -m recovery.cli eval --arms agent --provider groq          # replay, offline
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from recovery.agent import prompts, tools
from recovery.agent.runner import (
    DEFAULT_CASSETTE_ROOT,
    MAX_ITERATIONS,
    MODE_LIVE,
    MODE_RECORD,
    MODE_REPLAY,
    CassetteStore,
    PlanResult,
    request_key,
)
from recovery.agent.tools import Session
from recovery.policy.engine import Gate, rules_summary
from recovery.transport.base import CaseState, SimParams

MODEL = "openai/gpt-oss-120b"
"""Chosen from what a free Groq key actually reaches, not from a wish list.

It is the largest tool-calling model on that list with a 131k context window. The
smaller ``openai/gpt-oss-20b`` and ``qwen/qwen3.8-27b`` are the fallbacks if the daily
limit bites; both are selectable with ``--model``.
"""

MAX_TOKENS = 2000

DEFAULT_GROQ_CASSETTES = DEFAULT_CASSETTE_ROOT / "groq"
"""Groq recordings live beside the Claude ones, not among them."""

RATE_LIMIT_RETRIES = 6
RATE_LIMIT_BASE_WAIT_S = 2.0

# A weaker model needs the standing instruction repeated where it will act on it. The
# Anthropic system prompt is shared verbatim; this is appended, and it is appended for
# a measured reason -- without it the model narrates a plan in prose and never calls a
# tool, which scores as a case the agent silently declined to work.
SMALL_MODEL_ADDENDUM = """

# How to work this case

You MUST act by calling tools. Prose is not an action: a plan you describe but do not
schedule does not exist, and the case will be recorded as closed with nothing done.

1. Call `get_case_detail` first. Always. You cannot plan without the decline class.
2. Use `check_policy` before `schedule_retry` or `send_message` when you are unsure --
   it is free and it tells you exactly which rule would refuse you.
3. If a tool refuses your action, READ the rule id and the reason. Do not retry the
   same action unchanged; either fix what the rule objected to or move on.
4. When there is nothing left worth doing, call `close_case` with a reason.

Stop as soon as the plan is complete. Do not pad it with extra contacts."""


class GroqToolCallError(RuntimeError):
    """The model produced a tool call that could not be dispatched at all."""


def resolve_api_key() -> str:
    """``GROQ_API_KEY`` from the environment, falling back to ``.env``.

    Reuses the loader written for the Razorpay transport rather than adding
    ``python-dotenv``: the project already made that call once, and ``.env.example`` is
    the specification for both.
    """
    from recovery.transport.razorpay_test import load_env_file

    key = os.environ.get("GROQ_API_KEY") or load_env_file().get("GROQ_API_KEY")
    if not key:
        raise RuntimeError(
            "GROQ_API_KEY not set. It goes in .env (see .env.example); .gitignore "
            "excludes it. Replay needs no key at all -- drop --record to run offline "
            "from the committed cassettes."
        )
    return key


def tool_schemas() -> list[dict]:
    """OpenAI-shape tool definitions, generated from the Anthropic tool objects.

    ``@beta_tool`` already derived a JSON schema from each function's signature and
    docstring, and ``BetaFunctionTool`` exposes it. Re-deriving it here -- or worse,
    hand-writing a second copy -- would create two schemas that drift apart, and the
    drift would show up as a model calling a tool with arguments the gate rejects.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            },
        }
        for tool in tools.TOOLS
    ]


_BY_NAME = {tool.name: tool for tool in tools.TOOLS}


def dispatch(name: str, raw_arguments: str) -> str:
    """Run one tool call, returning the string the model sees next.

    Every failure here is returned **to the model** rather than raised, because all of
    them are things a weaker model actually does and all of them are recoverable: an
    invented tool name, malformed JSON arguments, a missing or misspelled parameter. The
    model gets told what went wrong and takes another turn. What is *not* recoverable --
    and so is not caught -- is an exception from inside the tool body, because that is
    the policy engine or the ledger failing and it must not be reported to a language
    model as a retryable inconvenience.
    """
    tool = _BY_NAME.get(name)
    if tool is None:
        return json.dumps(
            {"error": f"no tool named {name!r}", "available": sorted(_BY_NAME)},
            sort_keys=True,
        )

    try:
        arguments = json.loads(raw_arguments or "{}")
    except json.JSONDecodeError as exc:
        return json.dumps({"error": f"arguments were not valid JSON: {exc}"}, sort_keys=True)

    if not isinstance(arguments, dict):
        return json.dumps({"error": "arguments must be a JSON object"}, sort_keys=True)

    try:
        return str(tool.func(**arguments))
    except TypeError as exc:
        return json.dumps(
            {
                "error": f"wrong arguments for {name}: {exc}",
                "expected": sorted(tool.input_schema.get("properties", {})),
            },
            sort_keys=True,
        )


@dataclass
class GroqPlanner:
    """Turns one :class:`CaseState` into a policy-screened plan, via a Groq model.

    Deliberately the same shape as :class:`recovery.agent.runner.Planner` -- same
    constructor arguments, same ``plan()`` returning the same :class:`PlanResult` -- so
    ``recovery/arms/agent.py`` takes it through its existing ``options["planner"]`` seam
    with no change at all.
    """

    gate: Gate
    params: SimParams
    store: CassetteStore
    model: str = MODEL
    system: str = field(init=False)
    _client: Any = field(default=None, init=False, repr=False)
    _tools: list[dict] = field(default_factory=tool_schemas, init=False, repr=False)

    def __post_init__(self) -> None:
        # Rendered from the engine itself, exactly as the Anthropic path does, so the
        # rules the model reads cannot drift from the rules that gate it.
        base = prompts.system_prompt(list(rules_summary(self.gate.engine)), self.params)
        self.system = base + SMALL_MODEL_ADDENDUM

    @property
    def client(self):
        if self._client is None:
            from groq import Groq

            key = (
                resolve_api_key()
                if self.store.mode in (MODE_RECORD, MODE_LIVE)
                else "cassette-replay-no-network"
            )
            self._client = Groq(api_key=key)
        return self._client

    # -- the one network call -------------------------------------------------------

    def _create(self, **params):
        """Cassette-aware completion. A replay miss raises; it never goes live.

        The Anthropic runner has to patch a bound method because the SDK owns its loop.
        Here the loop is ours, so the seam is just this method -- one place to reason
        about, and no assumptions about the SDK's cached properties.
        """
        key = request_key(params)
        case_id = self.store.current_case

        if self.store.mode == MODE_REPLAY:
            from groq.types.chat import ChatCompletion

            return ChatCompletion.model_validate(self.store.fetch(case_id, key))

        response = self._call_with_backoff(params)
        if self.store.mode == MODE_RECORD:
            self.store.put(case_id, key, response.model_dump(mode="json"))
        return response

    def _call_with_backoff(self, params: dict):
        """Free-tier rate limits are the expected failure on a 500-case recording run."""
        from groq import RateLimitError

        for attempt in range(RATE_LIMIT_RETRIES):
            try:
                return self.client.chat.completions.create(**params)
            except RateLimitError as exc:
                if attempt == RATE_LIMIT_RETRIES - 1:
                    raise
                wait = RATE_LIMIT_BASE_WAIT_S * (2**attempt)
                retry_after = getattr(getattr(exc, "response", None), "headers", {}) or {}
                with_header = retry_after.get("retry-after")
                if with_header:
                    try:
                        wait = max(wait, float(with_header))
                    except (TypeError, ValueError):
                        pass
                print(f"  rate limited; waiting {wait:.0f}s (attempt {attempt + 1})")
                time.sleep(wait)
        raise RuntimeError("unreachable")

    # -- planning -------------------------------------------------------------------

    def plan(self, state: CaseState) -> PlanResult:
        session = Session(gate=self.gate, state=state, params=self.params)
        self.store.current_case = state.case_id
        token = tools.bind(session)
        try:
            stats = self._run(state)
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

    def _run(self, state: CaseState) -> dict:
        """The turn loop the Anthropic SDK would otherwise own."""
        messages: list[dict] = [
            {"role": "system", "content": self.system},
            {"role": "user", "content": prompts.case_brief(state)},
        ]

        turns = input_tokens = output_tokens = 0

        for _ in range(MAX_ITERATIONS):
            response = self._create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                messages=messages,
                tools=self._tools,
                tool_choice="auto",
            )
            turns += 1

            usage = getattr(response, "usage", None)
            if usage is not None:
                input_tokens += getattr(usage, "prompt_tokens", 0) or 0
                output_tokens += getattr(usage, "completion_tokens", 0) or 0

            message = response.choices[0].message
            calls = message.tool_calls or []
            if not calls:
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in calls
                    ],
                }
            )
            for call in calls:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": dispatch(call.function.name, call.function.arguments),
                    }
                )

        return {
            "turns": turns,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            # Groq exposes no prompt-cache counter, so this is structurally zero rather
            # than unmeasured. Reporting it as anything else would invent a saving.
            "cache_read_tokens": 0,
        }


def build_planner(
    gate: Gate,
    params: SimParams,
    *,
    mode: str = MODE_REPLAY,
    model: str = MODEL,
    cassette_root: Path | str = DEFAULT_CASSETTE_ROOT,
) -> GroqPlanner:
    """A planner wired to its own cassette directory, kept apart from the Claude ones.

    The two providers answer the same prompt differently, so their recordings are
    different artifacts and must not share a namespace -- a Groq cassette replayed into
    the Anthropic runner would be a silent cross-provider miss at best.
    """
    return GroqPlanner(
        gate=gate,
        params=params,
        store=CassetteStore(root=Path(cassette_root), mode=mode),
        model=model,
    )
