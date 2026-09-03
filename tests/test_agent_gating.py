"""The agent arm's gate, offline. Nothing here needs an API key.

The load-bearing assertions in this file are made **on the transport**, not on what a
tool returned. A gate that logs a denial and then acts anyway would satisfy a test that
only checked the return value, and it is precisely the bug that would make the
compliance story a lie while every test stayed green.

The model is replaced by a scripted cassette store, which fakes only the key lookup.
Everything else -- the SDK's tool runner, the tool functions, the policy engine, the
gate, the ledger, the simulator -- is the real production path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from recovery.agent import runner as runner_mod
from recovery.agent import tools as tools_mod
from recovery.agent.runner import CassetteMiss, CassetteStore, Planner, request_key
from recovery.agent.tools import Session
from recovery.arms.agent import AgentArm
from recovery.arms.control import ArmContext
from recovery.declines import Method
from recovery.ledger import KIND_ACTION, KIND_POLICY_CHECK, read_entries
from recovery.transport.base import SimParams
from tests.conftest import make_case, make_gate

# -- scripted model ------------------------------------------------------------------


def a_message(*blocks: dict, stop_reason: str = "tool_use") -> dict:
    """A synthetic assistant turn, in the shape ``ParsedBetaMessage`` validates."""
    return {
        "id": "msg_scripted",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": list(blocks),
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 40,
            "cache_read_input_tokens": 1100,
            "cache_creation_input_tokens": 0,
        },
    }


def calls(name: str, **tool_input) -> dict:
    return {"type": "tool_use", "id": f"toolu_{name}", "name": name, "input": tool_input}


def says(text: str) -> dict:
    return {"type": "text", "text": text}


def done(text: str = "Plan committed.") -> dict:
    return a_message(says(text), stop_reason="end_turn")


@dataclass
class ScriptedStore(CassetteStore):
    """A cassette store that answers every key from a per-case script.

    Only the key lookup is faked. The runner still builds a real request, computes a
    real key over it and validates a real response, so the tool loop under test is the
    one that runs in production.
    """

    script: dict[str, list[dict]] = field(default_factory=dict)
    keys_seen: list[str] = field(default_factory=list)

    def fetch(self, case_id: str, key: str) -> dict:
        self.keys_seen.append(key)
        turns = self.script.get(case_id)
        if not turns:
            raise CassetteMiss(f"script for {case_id} is exhausted")
        return turns.pop(0)


def scripted(case_id: str, *turns: dict) -> ScriptedStore:
    return ScriptedStore(script={case_id: list(turns)})


# -- fixtures --------------------------------------------------------------------------


def a_planner(tmp_path, cases, store, *, arm="agent", params=None):
    """A planner over a real gate, a real ledger and a real simulator."""
    params = params or SimParams()
    gate = make_gate(tmp_path, cases, arm=arm, params=params)
    return Planner(gate=gate, params=params, store=store), gate


def policy_checks(gate, *, allowed=None):
    gate.ledger._fh.flush()
    rows = [r for r in read_entries(gate.ledger.path) if r["kind"] == KIND_POLICY_CHECK]
    if allowed is None:
        return rows
    return [r for r in rows if r["allowed"] is allowed]


def action_entries(gate):
    gate.ledger._fh.flush()
    return [r for r in read_entries(gate.ledger.path) if r["kind"] == KIND_ACTION]


def denied_rule_ids(row) -> list[str]:
    return [v["rule_id"] for v in row["verdicts"] if not v["allow"]]


def tool_names(params: dict) -> list[str]:
    """Tool names as they reach the request; the SDK renders them to dicts on the way."""
    return [t["name"] if isinstance(t, dict) else t.name for t in params["tools"]]


# -- the load-bearing tests ------------------------------------------------------------


def test_hard_decline_retry_is_denied_and_never_reaches_the_transport(tmp_path):
    """The one that matters. Asserted on the transport, not on the return value."""
    case = make_case("case-hard", decline_reason="stolen_card", amount_paise=250_000)
    store = scripted(
        "case-hard",
        a_message(calls("schedule_retry", case_id="case-hard", at_hour=24, idempotency_key="r1")),
        done(),
    )
    planner, gate = a_planner(tmp_path, [case], store)
    state = gate.transport.observe("case-hard", 0)

    plan = planner.plan(state)

    assert plan.actions == ()
    assert plan.denials == 1

    # The transport saw nothing at all.
    outcome = {o.case_id: o for o in gate.transport.outcomes()}["case-hard"]
    assert outcome.attempts == 0
    assert outcome.cost_paise == 0

    # Exactly one denial in the ledger, naming the rule, and no action entry.
    denials = policy_checks(gate, allowed=False)
    assert len(denials) == 1
    assert "HardDeclineNoRetry" in denied_rule_ids(denials[0])
    assert action_entries(gate) == []


def test_quiet_hours_message_is_denied_and_never_reaches_the_transport(tmp_path):
    """21:00-09:00 IST. The case fails at 10:00, so hour 11 is 21:00 -- inside."""
    case = make_case("case-quiet", decline_reason="expired_card")
    store = scripted(
        "case-quiet",
        a_message(
            calls(
                "send_message",
                case_id="case-quiet",
                channel="sms",
                template="update_instrument",
                at_hour=11,
                idempotency_key="n1",
            )
        ),
        done(),
    )
    planner, gate = a_planner(tmp_path, [case], store)
    state = gate.transport.observe("case-quiet", 0)

    plan = planner.plan(state)

    assert plan.actions == ()
    outcome = {o.case_id: o for o in gate.transport.outcomes()}["case-quiet"]
    assert outcome.contacts == 0
    assert outcome.cost_paise == 0

    denials = policy_checks(gate, allowed=False)
    assert len(denials) == 1
    assert "QuietHours" in denied_rule_ids(denials[0])
    assert action_entries(gate) == []


def test_a_denial_writes_one_policy_check_with_a_rule_id_and_no_action(tmp_path):
    case = make_case("case-hard2", decline_reason="lost_card")
    store = scripted(
        "case-hard2",
        a_message(calls("schedule_retry", case_id="case-hard2", at_hour=48, idempotency_key="r1")),
        done(),
    )
    planner, gate = a_planner(tmp_path, [case], store)
    planner.plan(gate.transport.observe("case-hard2", 0))

    rows = policy_checks(gate)
    assert len(rows) == 1
    row = rows[0]
    assert row["allowed"] is False
    assert row["idempotency_key"] == "case-hard2:r1"
    assert denied_rule_ids(row), "a denial must name at least one objecting rule"
    # Every registered rule appears, because the engine does not short-circuit.
    assert len(row["verdicts"]) == 12
    assert action_entries(gate) == []


def test_check_policy_is_side_effect_free(tmp_path):
    """A dry run writes no action entry, schedules nothing and moves no money."""
    case = make_case("case-dry", decline_reason="insufficient_funds")
    store = scripted(
        "case-dry",
        a_message(
            calls("check_policy", case_id="case-dry", action_type="retry", at_hour=48),
            calls(
                "check_policy",
                case_id="case-dry",
                action_type="message",
                at_hour=11,
                channel="sms",
                template="payment_reminder",
            ),
        ),
        done("Just looking."),
    )
    planner, gate = a_planner(tmp_path, [case], store)
    plan = planner.plan(gate.transport.observe("case-dry", 0))

    assert plan.actions == ()
    assert plan.denials == 0, "a dry run is not a denial"
    assert action_entries(gate) == []
    assert policy_checks(gate) == [], "preview writes nothing, allowed or denied"

    outcome = {o.case_id: o for o in gate.transport.outcomes()}["case-dry"]
    assert (outcome.attempts, outcome.contacts, outcome.cost_paise) == (0, 0, 0)


def test_check_policy_agrees_with_the_screen_that_follows_it(tmp_path):
    """The dry run must not tell the model something the real gate then contradicts."""
    case = make_case("case-agree", decline_reason="insufficient_funds", amount_paise=200_000)
    gate = make_gate(tmp_path, [case], arm="agent")
    state = gate.transport.observe("case-agree", 0)
    session = Session(gate=gate, state=state, params=SimParams())
    token = tools_mod.bind(session)
    try:
        preview = json.loads(
            tools_mod.check_policy.call(
                {"case_id": "case-agree", "action_type": "retry", "at_hour": 48}
            )
        )
        real = json.loads(
            tools_mod.schedule_retry.call(
                {"case_id": "case-agree", "at_hour": 48, "idempotency_key": "r1"}
            )
        )
    finally:
        tools_mod.unbind(token)

    assert preview["allowed"] == real["allowed"]
    assert [v["rule_id"] for v in preview["verdicts"]] == [v["rule_id"] for v in real["verdicts"]]


# -- the gate seen from the tool's own return value --------------------------------------


def test_a_denied_tool_returns_a_refusal_rather_than_raising(tmp_path):
    """Denials are return values. Raising would abort the turn and teach the model
    nothing, which is the whole reason the gate lives inside the tool."""
    case = make_case("case-ret", decline_reason="account_closed")
    gate = make_gate(tmp_path, [case], arm="agent")
    state = gate.transport.observe("case-ret", 0)
    session = Session(gate=gate, state=state, params=SimParams())
    token = tools_mod.bind(session)
    try:
        payload = json.loads(
            tools_mod.schedule_retry.call(
                {"case_id": "case-ret", "at_hour": 24, "idempotency_key": "r1"}
            )
        )
    finally:
        tools_mod.unbind(token)

    assert payload["allowed"] is False
    assert payload["scheduled"] is False
    assert payload["rule_id"] == "HardDeclineNoRetry"
    assert payload["reason"]
    assert session.planned == []


def test_out_of_window_and_malformed_arguments_return_errors(tmp_path):
    case = make_case("case-bad", decline_reason="insufficient_funds")
    gate = make_gate(tmp_path, [case], arm="agent")
    session = Session(gate=gate, state=gate.transport.observe("case-bad", 0), params=SimParams())
    token = tools_mod.bind(session)
    try:
        beyond = json.loads(
            tools_mod.schedule_retry.call(
                {"case_id": "case-bad", "at_hour": 9999, "idempotency_key": "r1"}
            )
        )
        wrong_case = json.loads(
            tools_mod.schedule_retry.call(
                {"case_id": "case-elsewhere", "at_hour": 24, "idempotency_key": "r1"}
            )
        )
        unknown_channel = json.loads(
            tools_mod.send_message.call(
                {
                    "case_id": "case-bad",
                    "channel": "carrier_pigeon",
                    "template": "payment_reminder",
                    "at_hour": 24,
                    "idempotency_key": "n1",
                }
            )
        )
    finally:
        tools_mod.unbind(token)

    for payload in (beyond, wrong_case, unknown_channel):
        assert payload["ok"] is False
        assert payload["error"]
    assert session.planned == []


def test_a_malformed_tool_call_does_not_raise_through_the_runner(tmp_path):
    """The model omitting a required argument must cost it a turn, not the run."""
    case = make_case("case-malformed", decline_reason="insufficient_funds")
    store = scripted(
        "case-malformed",
        # No at_hour, so the SDK cannot bind the call at all.
        a_message(calls("schedule_retry", case_id="case-malformed", idempotency_key="r1")),
        done("Understood."),
    )
    planner, gate = a_planner(tmp_path, [case], store)

    plan = planner.plan(gate.transport.observe("case-malformed", 0))

    assert plan.actions == ()
    assert plan.turns == 2
    assert action_entries(gate) == []


# -- planning that is allowed to succeed ---------------------------------------------------


def test_an_allowed_plan_is_screened_then_executed_at_its_scheduled_hour(tmp_path):
    case = make_case("case-soft", decline_reason="insufficient_funds", amount_paise=300_000)
    store = scripted(
        "case-soft",
        a_message(calls("get_case_detail", case_id="case-soft")),
        a_message(calls("schedule_retry", case_id="case-soft", at_hour=48, idempotency_key="r1")),
        done(),
    )
    planner, gate = a_planner(tmp_path, [case], store)
    plan = planner.plan(gate.transport.observe("case-soft", 0))

    assert [a.at_hour for a in plan.actions] == [48]
    assert plan.actions[0].idempotency_key == "case-soft:r1"
    # Screened, not executed: an approval writes nothing and touches no transport.
    assert action_entries(gate) == []
    assert {o.case_id: o for o in gate.transport.outcomes()}["case-soft"].attempts == 0


def test_an_emandate_plan_can_schedule_the_notice_its_own_debit_requires(tmp_path):
    """The provisional index is what makes this possible.

    Screened against the real history, the debit at hour 72 would be refused for want
    of the pre-debit notice that the very same plan schedules at hour 24.
    """
    case = make_case(
        "case-em",
        decline_reason="insufficient_funds",
        method=Method.EMANDATE,
        amount_paise=400_000,
    )
    store = scripted(
        "case-em",
        a_message(
            calls(
                "send_message",
                case_id="case-em",
                channel="sms",
                template="pre_debit_notice",
                at_hour=24,
                idempotency_key="notice-1",
                debit_at_hour=72,
            )
        ),
        a_message(calls("schedule_retry", case_id="case-em", at_hour=72, idempotency_key="r1")),
        done(),
    )
    planner, gate = a_planner(tmp_path, [case], store)
    plan = planner.plan(gate.transport.observe("case-em", 0))

    assert [(a.type, a.at_hour) for a in plan.actions] == [("message", 24), ("retry", 72)]
    assert plan.denials == 0

    # And the notice carries every field the regulation requires.
    notice = plan.actions[0]
    assert set(notice.metadata) == {
        "merchant_name",
        "amount_paise",
        "debit_datetime",
        "mandate_reference",
    }


def test_afa_required_debits_cannot_be_self_certified(tmp_path):
    """No tool can set ``afa_completed``. A first debit is contact-only, for every arm."""
    case = make_case("case-afa", decline_reason="insufficient_funds", mandate_first_debit=True)
    gate = make_gate(tmp_path, [case], arm="agent")
    session = Session(gate=gate, state=gate.transport.observe("case-afa", 0), params=SimParams())
    token = tools_mod.bind(session)
    try:
        payload = json.loads(
            tools_mod.schedule_retry.call(
                {"case_id": "case-afa", "at_hour": 48, "idempotency_key": "r1"}
            )
        )
    finally:
        tools_mod.unbind(token)

    assert payload["allowed"] is False
    assert "RBIAdditionalFactorAuth" in payload["all_denials"]


def test_close_case_refuses_to_discard_a_plan(tmp_path):
    case = make_case("case-close", decline_reason="insufficient_funds", amount_paise=300_000)
    gate = make_gate(tmp_path, [case], arm="agent")
    session = Session(gate=gate, state=gate.transport.observe("case-close", 0), params=SimParams())
    token = tools_mod.bind(session)
    try:
        tools_mod.schedule_retry.call(
            {"case_id": "case-close", "at_hour": 48, "idempotency_key": "r1"}
        )
        payload = json.loads(
            tools_mod.close_case.call({"case_id": "case-close", "reason": "changed my mind"})
        )
    finally:
        tools_mod.unbind(token)

    assert payload["ok"] is False
    assert session.closed is False
    assert len(session.planned) == 1


def test_idempotency_keys_do_not_collide_across_cases(tmp_path):
    """``LedgerIndex.used_keys`` is global, so an unscoped key would deny case two."""
    cases = [
        make_case("case-a", decline_reason="insufficient_funds", amount_paise=300_000),
        make_case("case-b", decline_reason="insufficient_funds", amount_paise=300_000),
    ]
    gate = make_gate(tmp_path, cases, arm="agent")
    results = {}
    for case_id in ("case-a", "case-b"):
        session = Session(
            gate=gate, state=gate.transport.observe(case_id, 0), params=SimParams()
        )
        token = tools_mod.bind(session)
        try:
            # Both cases choose the same name, which is the realistic failure.
            results[case_id] = json.loads(
                tools_mod.schedule_retry.call(
                    {"case_id": case_id, "at_hour": 48, "idempotency_key": "retry-1"}
                )
            )
        finally:
            tools_mod.unbind(token)

    assert results["case-a"]["allowed"] is True
    assert results["case-b"]["allowed"] is True
    assert results["case-a"]["idempotency_key"] != results["case-b"]["idempotency_key"]


# -- cassettes ----------------------------------------------------------------------------


def test_a_cassette_miss_in_replay_raises_and_never_calls_the_api(tmp_path, monkeypatch):
    """Asserted with no credential in the environment, which is the reviewer's case."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)

    case = make_case("case-miss", decline_reason="insufficient_funds")
    store = CassetteStore(root=tmp_path / "cassettes", mode="replay")
    planner, gate = a_planner(tmp_path, [case], store)

    with pytest.raises(CassetteMiss) as excinfo:
        planner.plan(gate.transport.observe("case-miss", 0))
    assert "--record" in str(excinfo.value)


def test_the_request_key_is_canonical(tmp_path):
    """Two logically identical requests hash the same however their dicts were built."""
    first = {
        "model": "claude-opus-5",
        "max_tokens": 8000,
        "system": [{"type": "text", "text": "stable"}],
        "messages": [{"role": "user", "content": "hello"}],
    }
    second = {
        "messages": [{"content": "hello", "role": "user"}],
        "system": [{"text": "stable", "type": "text"}],
        "max_tokens": 8000,
        "model": "claude-opus-5",
    }
    assert request_key(first) == request_key(second)

    # And fields that cannot change the answer are excluded from the key.
    with_noise = {**first, "timeout": 30, "extra_headers": {"x-request-id": "abc"}}
    assert request_key(with_noise) == request_key(first)

    # While one that can, changes it.
    assert request_key({**first, "max_tokens": 4000}) != request_key(first)


def test_record_then_replay_reproduces_the_plan_exactly(tmp_path, monkeypatch):
    """The reproducibility claim, end to end, without ever calling the API.

    A scripted store stands in for the network on the recording pass; the replay pass
    reads the file that pass wrote, through the real replay path.
    """
    case = make_case("case-rt", decline_reason="insufficient_funds", amount_paise=300_000)
    turns = [
        a_message(calls("get_case_detail", case_id="case-rt")),
        a_message(calls("schedule_retry", case_id="case-rt", at_hour=48, idempotency_key="r1")),
        done(),
    ]

    root = tmp_path / "cassettes"

    class RecordingStore(ScriptedStore):
        """Replays from the script but writes what it served, as a record run would."""

        def fetch(self, case_id: str, key: str) -> dict:
            response = super().fetch(case_id, key)
            self.put(case_id, key, response)
            return response

    recording = RecordingStore(root=root, mode="replay", script={"case-rt": list(turns)})
    planner, gate = a_planner(tmp_path / "rec", [case], recording)
    recorded_plan = planner.plan(gate.transport.observe("case-rt", 0))
    recording.flush("case-rt")

    assert (root / "case-rt.json").exists()

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    replaying = CassetteStore(root=root, mode="replay")
    planner2, gate2 = a_planner(tmp_path / "replay", [case], replaying)
    replayed_plan = planner2.plan(gate2.transport.observe("case-rt", 0))

    assert [a.to_dict() for a in replayed_plan.actions] == [
        a.to_dict() for a in recorded_plan.actions
    ]
    assert replayed_plan.turns == recorded_plan.turns


# -- the arm ---------------------------------------------------------------------------------


def an_arm(tmp_path, cases, store, *, params=None, arm_name="agent"):
    from recovery.ledger import Ledger
    from recovery.policy.engine import Gate, default_engine
    from recovery.scoring import Scorer
    from recovery.transport.mock import MockTransport

    params = params or SimParams()
    transport = MockTransport(cases, seed=42, params=params)
    ledger = Ledger(tmp_path / f"{arm_name}.jsonl", run_id="run-test")
    scorer = Scorer(params=params)
    gate = Gate(
        engine=default_engine(scorer),
        ledger=ledger,
        transport=transport,
        arm=arm_name,
        params=params,
    )
    ctx = ArmContext(
        arm=arm_name,
        run_id="run-test",
        seed=42,
        params=params,
        transport=transport,
        ledger=ledger,
        gate=gate,
        scorer=scorer,
        options={"planner": Planner(gate=gate, params=params, store=store)},
    )
    return AgentArm(ctx), transport, ledger


def drive_arm(arm, transport, cases, params):
    from recovery.eval.harness import drive

    drive(arm, transport, cases, params)


def test_the_arm_executes_its_plan_at_the_scheduled_hour(tmp_path):
    case = make_case(
        "case-run",
        decline_reason="insufficient_funds",
        amount_paise=300_000,
        p_self_heal=0.0,
        retry_success_base=0.0,  # so the retry fires, fails, and the case stays open
        intent_to_pay=1.0,
    )
    store = scripted(
        "case-run",
        a_message(calls("schedule_retry", case_id="case-run", at_hour=48, idempotency_key="r1")),
        done(),
    )
    arm, transport, ledger = an_arm(tmp_path, [case], store)
    drive_arm(arm, transport, [case], SimParams())
    ledger.close()

    outcome = {o.case_id: o for o in transport.outcomes()}["case-run"]
    assert outcome.attempts == 1
    assert outcome.cost_paise == 300

    actions = [r for r in read_entries(ledger.path) if r["kind"] == KIND_ACTION]
    assert len(actions) == 1
    assert actions[0]["action"]["at_hour"] == 48
    assert actions[0]["idempotency_key"] == "case-run:r1"


def test_two_replay_runs_produce_identical_per_case_outcomes(tmp_path):
    """Determinism, at the level the published number is computed from."""
    cases = [
        make_case("case-d1", decline_reason="insufficient_funds", amount_paise=300_000),
        make_case("case-d2", decline_reason="stolen_card", amount_paise=120_000),
    ]

    def one_run(tag: str):
        store = ScriptedStore(
            script={
                "case-d1": [
                    a_message(
                        calls(
                            "schedule_retry",
                            case_id="case-d1",
                            at_hour=48,
                            idempotency_key="r1",
                        )
                    ),
                    done(),
                ],
                "case-d2": [
                    a_message(calls("close_case", case_id="case-d2", reason="hard decline")),
                    done(),
                ],
            }
        )
        arm, transport, ledger = an_arm(tmp_path / tag, cases, store)
        drive_arm(arm, transport, cases, SimParams())
        ledger.close()
        return [o.to_dict() for o in transport.outcomes()]

    assert one_run("a") == one_run("b")


def test_the_arm_closes_a_hard_decline_without_spending_anything(tmp_path):
    case = make_case("case-shut", decline_reason="stolen_card", amount_paise=250_000, p_self_heal=0.0)
    store = scripted(
        "case-shut",
        a_message(calls("close_case", case_id="case-shut", reason="stolen card, nothing works")),
        done(),
    )
    arm, transport, ledger = an_arm(tmp_path, [case], store)
    drive_arm(arm, transport, [case], SimParams())
    ledger.close()

    outcome = {o.case_id: o for o in transport.outcomes()}["case-shut"]
    assert (outcome.attempts, outcome.contacts, outcome.cost_paise) == (0, 0, 0)
    assert outcome.recovered is False

    actions = [r for r in read_entries(ledger.path) if r["kind"] == KIND_ACTION]
    assert [r["action"]["type"] for r in actions] == ["close"]


def test_the_agent_arm_refuses_to_run_without_a_gate(tmp_path):
    ctx = ArmContext(
        arm="agent",
        run_id="run-test",
        seed=42,
        params=SimParams(),
        transport=None,  # type: ignore[arg-type]
        ledger=None,  # type: ignore[arg-type]
        gate=None,
    )
    with pytest.raises(ValueError, match="policy gate"):
        AgentArm(ctx)


def code_of(path) -> str:
    """A module's executable source, with comments and string literals removed.

    Scanning the raw text is useless here: this layer's docstrings *warn against* the
    very identifiers the test is looking for, so a plain substring search flags the
    documentation that exists to prevent the bug.
    """
    import io
    import tokenize

    kept = []
    with path.open("rb") as fh:
        for token in tokenize.tokenize(io.BytesIO(fh.read()).readline):
            if token.type in (tokenize.COMMENT, tokenize.STRING):
                continue
            kept.append(token.string)
    return " ".join(kept)


def test_no_wall_clock_and_no_agent_sdk_in_the_agent_layer():
    """The three reversions this phase is most likely to suffer, asserted rather than
    trusted to review."""
    import pathlib

    import recovery.arms.agent as agent_arm_mod

    paths = sorted(pathlib.Path(runner_mod.__file__).parent.glob("*.py"))
    paths.append(pathlib.Path(agent_arm_mod.__file__))

    assert paths, "the scan found no modules, which would make it vacuous"
    for path in paths:
        code = code_of(path)
        assert "datetime . now" not in code, path
        assert "claude_agent_sdk" not in code, path
        assert "budget_tokens" not in code, path


def test_the_request_is_shaped_for_the_prompt_cache(tmp_path, monkeypatch):
    """The last thing that can only be checked on a recording run, checked offline.

    Caching is a prefix match over ``tools`` then ``system`` then ``messages``, so this
    asserts the two things that silently destroy it: an unsorted tool list, and a
    breakpoint that is not at the end of the stable prefix. A cache that quietly stops
    working costs real money and shows up nowhere except the bill.
    """
    captured: list[dict] = []
    real_request_key = runner_mod.request_key

    def capturing(params: dict) -> str:
        captured.append(params)
        return real_request_key(params)

    monkeypatch.setattr(runner_mod, "request_key", capturing)

    case = make_case("case-cache", decline_reason="insufficient_funds")
    store = scripted("case-cache", done("Nothing to do."))
    planner, gate = a_planner(tmp_path, [case], store)
    planner.plan(gate.transport.observe("case-cache", 0))

    assert len(captured) == 1
    params = captured[0]

    assert params["model"] == "claude-opus-5"
    assert params["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in json.dumps(params["thinking"])

    # The cache breakpoint sits at the end of the stable prefix, not inside it.
    system = params["system"]
    assert isinstance(system, list) and len(system) == 1
    assert system[0]["cache_control"] == {"type": "ephemeral"}

    # Six tools, in name order, so the prefix is byte-identical across cases.
    names = tool_names(params)
    assert names == sorted(names)
    assert set(names) == {
        "check_policy",
        "close_case",
        "get_case_detail",
        "get_customer_history",
        "schedule_retry",
        "send_message",
    }

    # Everything that varies per case is after the breakpoint, in the user message.
    assert params["messages"][0]["role"] == "user"
    assert "case-cache" in params["messages"][0]["content"]
    assert "case-cache" not in system[0]["text"], "a case id in the prefix breaks the cache"


def test_the_cached_prefix_is_identical_across_cases(tmp_path, monkeypatch):
    """Two different cases must produce the same bytes before the breakpoint."""
    captured: list[dict] = []
    real_request_key = runner_mod.request_key
    monkeypatch.setattr(
        runner_mod,
        "request_key",
        lambda params: (captured.append(params), real_request_key(params))[1],
    )

    cases = [
        make_case("case-p1", decline_reason="insufficient_funds", amount_paise=100_000),
        make_case("case-p2", decline_reason="expired_card", amount_paise=900_000),
    ]
    store = ScriptedStore(script={"case-p1": [done()], "case-p2": [done()]})
    planner, gate = a_planner(tmp_path, cases, store)
    for case_id in ("case-p1", "case-p2"):
        planner.plan(gate.transport.observe(case_id, 0))

    assert len(captured) == 2
    first, second = captured
    assert first["system"] == second["system"]
    assert tool_names(first) == tool_names(second)
    assert first["messages"] != second["messages"], "the per-case brief must differ"
