"""Determinism and common-random-number properties of the cohort generator.

These tests are not hygiene. If ``u()`` changes shape, or if a sequential RNG creeps
into the generation loop, the pairing between arms silently breaks and every published
number in this submission becomes invalid without anything visibly failing. That is
what these assertions are here to catch.
"""

from __future__ import annotations

from datetime import datetime

from recovery.cohort import (
    IST,
    Case,
    generate_cohort,
    latents_path_for,
    load_cohort,
    serialise_cohort,
    u,
    write_cohort,
)
from recovery.declines import Method, lookup


# --------------------------------------------------------------------------------
# The CRN helper
# --------------------------------------------------------------------------------


def test_u_is_stable_against_a_known_tuple():
    """A hard-coded expectation. If the hash input format ever changes -- a different
    separator, a different field order, blake2b digest bytes -- every number this
    project has published becomes incomparable with every number it publishes next.
    This test is the tripwire for that."""
    assert u(42, "case-00237", "self_heal", 5) == 0.9988711745209607
    assert u(42, "case-00000", "method", 0) == 0.3233661460529665
    assert u(7, "case-00001", "retry", 3) == 0.34550861552347273


def test_u_is_bounded():
    values = [u(42, f"case-{i:05d}", "probe", 0) for i in range(2000)]
    assert all(0.0 <= v < 1.0 for v in values)


def test_u_is_order_independent():
    """The whole point of common random numbers: a draw depends on the identity of the
    event, not on how many draws preceded it. Drawing B first must not perturb A."""
    a_first = u(42, "case-A", "self_heal", 3)
    _ = [u(42, "case-B", "self_heal", i) for i in range(50)]
    a_after = u(42, "case-A", "self_heal", 3)
    assert a_first == a_after


def test_u_varies_across_every_key_component():
    base = u(42, "case-00001", "self_heal", 0)
    assert u(43, "case-00001", "self_heal", 0) != base
    assert u(42, "case-00002", "self_heal", 0) != base
    assert u(42, "case-00001", "retry", 0) != base
    assert u(42, "case-00001", "self_heal", 1) != base


# --------------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------------


def test_same_seed_produces_byte_identical_cohort():
    """Serialise both and compare bytes rather than fields: a field-by-field check
    would miss ordering drift, which is exactly what breaks the reproduction gate."""
    first = serialise_cohort(generate_cohort(seed=42, n=200)).encode("utf-8")
    second = serialise_cohort(generate_cohort(seed=42, n=200)).encode("utf-8")
    assert first == second


def test_different_seed_produces_a_different_cohort():
    a = serialise_cohort(generate_cohort(seed=42, n=200))
    b = serialise_cohort(generate_cohort(seed=43, n=200))
    assert a != b


def test_cohort_is_a_stable_prefix_as_n_grows():
    """Case i is a pure function of (seed, i), so enlarging the cohort must not
    reshuffle the cases already in it."""
    small = generate_cohort(seed=42, n=50)
    large = generate_cohort(seed=42, n=200)
    assert [c.case_id for c in small] == [c.case_id for c in large[:50]]
    assert [c.amount_paise for c in small] == [c.amount_paise for c in large[:50]]


# --------------------------------------------------------------------------------
# Money and schema invariants
# --------------------------------------------------------------------------------


def test_all_amounts_are_integer_paise():
    for case in generate_cohort(seed=42, n=500):
        assert type(case.amount_paise) is int, case.case_id
        assert not isinstance(case.amount_paise, float)
        assert case.amount_paise > 0


def test_every_decline_reason_is_valid_for_its_method():
    for case in generate_cohort(seed=42, n=500):
        reason = lookup(case.decline_reason)
        assert case.method in reason.methods, f"{case.decline_reason} on {case.method}"


def test_timestamps_are_ist_and_deterministic():
    for case in generate_cohort(seed=42, n=100):
        assert isinstance(case.failed_at, datetime)
        assert case.failed_at.utcoffset() == IST.utcoffset(None)


def test_customers_are_shared_across_cases_with_one_personality_each():
    """Fatigue rules in phase 4 only mean something if a customer can own more than
    one failed payment -- and that customer must look the same in each of them."""
    cases = generate_cohort(seed=42, n=500)
    seen: dict[str, str] = {}
    for case in cases:
        cid = case.customer.customer_id
        if cid in seen:
            assert seen[cid] == case.customer.segment
        seen[cid] = case.customer.segment
    assert len(seen) < len(cases), "expected some customers to carry several cases"


def test_latents_are_present_at_generation_time():
    for case in generate_cohort(seed=42, n=50):
        assert case.latents is not None
        assert 0.0 <= case.latents.p_self_heal <= 1.0
        assert set(case.latents.channel_affinity) == {"sms", "whatsapp", "email"}


def test_hard_declines_carry_no_recovery_probability_at_all():
    for case in generate_cohort(seed=42, n=500):
        reason = lookup(case.decline_reason)
        assert case.latents is not None
        if reason.klass.value == "hard":
            assert case.latents.retry_success_base == 0.0
            assert case.latents.p_self_heal == 0.0


# --------------------------------------------------------------------------------
# Latents are hidden -- structurally, not by convention
# --------------------------------------------------------------------------------


def test_serialised_cohort_contains_no_latents():
    """An arm reading the committed cohort file cannot see the answer, because the
    answer is not in the file."""
    text = serialise_cohort(generate_cohort(seed=42, n=100))
    for leak in ("p_self_heal", "retry_success_base", "intent_to_pay", "responsiveness", "channel_affinity"):
        assert leak not in text, f"{leak} leaked into the arm-facing cohort"


def test_load_cohort_hides_latents_by_default(tmp_path):
    path = tmp_path / "cohort.jsonl"
    write_cohort(generate_cohort(seed=42, n=25), path)

    arm_view = load_cohort(path)
    assert all(c.latents is None for c in arm_view)

    sim_view = load_cohort(path, with_latents=True)
    assert all(c.latents is not None for c in sim_view)


def test_round_trip_preserves_the_public_fields(tmp_path):
    path = tmp_path / "cohort.jsonl"
    original = generate_cohort(seed=42, n=25)
    write_cohort(original, path)
    reloaded = load_cohort(path, with_latents=True)

    assert original == reloaded


def test_write_cohort_places_latents_in_a_separate_file(tmp_path):
    path = tmp_path / "cohort.jsonl"
    write_cohort(generate_cohort(seed=42, n=10), path)
    side = latents_path_for(path)
    assert side.exists()
    assert side != path
    assert side.name == "cohort.latents.jsonl"


def test_written_file_is_byte_identical_on_regeneration(tmp_path):
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    write_cohort(generate_cohort(seed=42, n=100), a)
    write_cohort(generate_cohort(seed=42, n=100), b)
    assert a.read_bytes() == b.read_bytes()
    assert latents_path_for(a).read_bytes() == latents_path_for(b).read_bytes()


def test_case_is_frozen():
    case = generate_cohort(seed=42, n=1)[0]
    assert isinstance(case, Case)
    try:
        case.amount_paise = 1  # type: ignore[misc]
    except Exception as exc:  # dataclasses raises FrozenInstanceError
        assert "frozen" in type(exc).__name__.lower() or "frozen" in str(exc).lower()
    else:
        raise AssertionError("Case must be immutable")


def test_method_mix_is_upi_heavy():
    cases = generate_cohort(seed=42, n=500)
    upi = sum(1 for c in cases if c.method is Method.UPI)
    assert upi / len(cases) > 0.4


def test_eval_never_shrinks_a_committed_cohort_artifact(tmp_path, monkeypatch, capsys):
    """A development run at --n 20 must not replace the published 500-case artifact.

    The cohort is a pure function of (seed, n), so a smaller run is perfectly valid --
    the danger is entirely to the file on disk. Overwriting it would leave the
    reproduction gate passing against a cohort that is not the one the README reports.
    """
    from recovery.cli import _cohort_for_eval

    monkeypatch.chdir(tmp_path)
    published = write_cohort(generate_cohort(seed=42, n=500), tmp_path / "data" / "cohort_seed42.jsonl")
    before = published.read_bytes()

    cases = _cohort_for_eval(seed=42, n=20)

    assert len(cases) == 20, "the run itself still uses the size that was asked for"
    assert published.read_bytes() == before, "the committed artifact must be untouched"
    assert "left" in capsys.readouterr().out, "and the run must say so, not do it silently"


def test_eval_writes_the_cohort_artifact_when_there_is_none(tmp_path, monkeypatch):
    """A clean clone still gets a self-contained eval."""
    from recovery.cli import _cohort_for_eval

    monkeypatch.chdir(tmp_path)
    cases = _cohort_for_eval(seed=42, n=25)

    written = tmp_path / "data" / "cohort_seed42.jsonl"
    assert written.exists()
    assert len(written.read_text(encoding="utf-8").strip().splitlines()) == 25
    assert len(cases) == 25
