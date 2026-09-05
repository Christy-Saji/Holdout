"""The generated readout must not be able to drift from the numbers it reports.

``docs/index.html`` is generated, never hand-edited, and it is the artifact a reviewer
is most likely to look at first. Two ways it can go quietly wrong, both caught here:

* the template's script reads an element id the markup no longer has, so a section
  renders empty and nothing errors loudly enough to notice;
* the payload loses a key the page draws from, so a chart silently disappears.

The viewer is **not** part of the measurement pipeline -- nothing in ``recovery/``
imports it and deleting it changes no published number -- so these are cheap structural
checks rather than an assertion about any figure.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "scripts" / "viewer.template.html"
GENERATED = ROOT / "docs" / "index.html"
RUN_DIR = ROOT / "data" / "results" / "run-s42-n500"

PLACEHOLDER = "__DATA__"

DRAWN_KEYS = frozenset(
    {"run", "arms", "ci", "bootstrap_replicates", "timing", "causes", "denial", "chain",
     "costs", "sweep", "sweeps", "sweep_order", "cases", "timelines"}
)
"""Every top-level payload key the page draws from. ``bootstrap_replicates``, ``timing``
and ``costs`` are in this set deliberately: all three were being computed and inlined
while the page drew none of them, which is a payload paying for figures nobody sees."""


@pytest.fixture(scope="module")
def builder():
    """``scripts/build_viewer.py`` by path -- ``scripts/`` is not an importable package."""
    spec = importlib.util.spec_from_file_location(
        "build_viewer", ROOT / "scripts" / "build_viewer.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def script_of(html: str) -> str:
    """The page's own code, excluding the inlined JSON payload."""
    return "\n".join(
        re.findall(r"<script(?![^>]*application/json)[^>]*>(.*?)</script>", html, re.S)
    )


def test_the_template_has_exactly_one_data_placeholder():
    assert TEMPLATE.read_text(encoding="utf-8").count(PLACEHOLDER) == 1


def test_every_element_the_script_reaches_for_exists_in_the_markup():
    """A renamed id is otherwise invisible: the section just renders empty."""
    html = TEMPLATE.read_text(encoding="utf-8")
    js = script_of(html)
    ids = set(re.findall(r'\bid="([^"]+)"', html))
    wanted = set(re.findall(r'\$\("([^"]+)"\)', js)) | set(
        re.findall(r'getElementById\("([^"]+)"\)', js)
    )
    assert wanted - ids == set()


def test_the_payload_carries_every_key_the_page_draws(builder):
    payload = builder.collect(RUN_DIR)
    assert DRAWN_KEYS - set(payload) == set()
    js = script_of(TEMPLATE.read_text(encoding="utf-8"))
    unread = {k for k in DRAWN_KEYS if f"D.{k}" not in js and f'D["{k}"]' not in js}
    assert unread == set(), f"payload keys computed and inlined but never drawn: {unread}"


def test_the_generated_page_substitutes_and_parses(builder, tmp_path, monkeypatch):
    """Build into a temp file: a test must not rewrite the committed artifact."""
    out = tmp_path / "index.html"
    monkeypatch.setattr(builder, "OUT", out)
    assert builder.main() == 0

    html = out.read_text(encoding="utf-8")
    assert PLACEHOLDER not in html, "the data placeholder survived into the output"

    blob = re.search(
        r'<script id="payload" type="application/json">(.*?)</script>', html, re.S
    ).group(1)
    # The builder escapes "</" so a value can never close the script element early.
    assert "</" not in blob
    payload = json.loads(blob.replace("<\\/", "</"))
    assert payload["run"]["n"] == 500
    assert len(payload["bootstrap_replicates"]) == payload["ci"]["baseline"]["replicates"]


def test_the_generated_page_is_byte_stable(builder, tmp_path, monkeypatch):
    """It is committed, so two builds of the same run must produce the same bytes."""
    first, second = tmp_path / "a.html", tmp_path / "b.html"
    for out in (first, second):
        monkeypatch.setattr(builder, "OUT", out)
        assert builder.main() == 0
    assert first.read_bytes() == second.read_bytes()


def test_the_committed_page_matches_the_current_template(builder, tmp_path, monkeypatch):
    """``docs/index.html`` is generated; a hand-edit or a stale build fails here."""
    if not GENERATED.exists():
        pytest.skip("no generated viewer committed")
    out = tmp_path / "index.html"
    monkeypatch.setattr(builder, "OUT", out)
    assert builder.main() == 0
    assert out.read_bytes() == GENERATED.read_bytes(), (
        "docs/index.html is stale or hand-edited; regenerate with "
        "`python scripts/build_viewer.py`"
    )


# -- the replay payload ---------------------------------------------------------------


def test_the_replay_carries_every_case_and_both_arms(builder):
    payload = builder.collect(RUN_DIR)

    assert len(payload["cases"]) == payload["run"]["n"]
    assert set(payload["timelines"]) == {a["arm"] for a in payload["arms"]}
    for arm in payload["timelines"].values():
        assert len(arm) == payload["run"]["n"], "a case with no timeline replays as blank"


def test_the_replay_payload_never_carries_a_latent(builder):
    """The holdout argument is circular the moment the page can show hidden state.

    ``Case.latents`` is readable only by the simulator. The viewer builds its case index
    through ``load_cohort`` without the latents side table -- the same call every arm
    makes -- so this asserts the outcome of that choice rather than trusting it.
    """
    payload = builder.collect(RUN_DIR)
    blob = json.dumps(payload)

    assert "latent" not in blob.lower()
    for case in payload["cases"]:
        assert "latents" not in case
        assert set(case) == {
            "id", "amount", "method", "reason", "recurring",
            "first_debit", "category", "arms",
        }


def test_the_replay_keeps_refusals_and_drops_redundant_allowed_checks(builder):
    """An allowed check duplicates the action it authorised; a refusal is the point."""
    payload = builder.collect(RUN_DIR)
    treated = next(a["arm"] for a in payload["arms"] if a["arm"] != "control")
    entries = [e for case in payload["timelines"][treated].values() for e in case]

    checks = [e for e in entries if e["kind"] == "policy_check"]
    assert checks, "no policy checks survived; the refusal lane would be empty"
    assert all(e.get("denied") for e in checks), "an allowed check was inlined for nothing"

    denied_total = sum(len(e["denied"]) for e in checks)
    blocked = next(a for a in payload["arms"] if a["arm"] == treated)["blocked_by_rule"]
    assert denied_total == sum(blocked.values()), "the replay and the ledger disagree"


def test_the_sweep_selector_offers_the_contested_parameter_first(builder):
    """Payload keys are sorted for byte-stability, so the order travels separately."""
    payload = builder.collect(RUN_DIR)
    assert payload["sweep_order"], "no sweeps committed"
    assert payload["sweep_order"][0] == "p_self_heal"
    assert set(payload["sweep_order"]) <= set(payload["sweeps"])


def test_the_page_needs_no_network(builder, tmp_path, monkeypatch):
    """The README promises a page that needs no network. It used to fetch its fonts.

    Opened offline, every face fell back to the system UI font and the design collapsed
    -- which is what a reviewer would have seen. Nothing may reference an external host
    again; the SVG namespace is a bare identifier, not a fetch.
    """
    out = tmp_path / "index.html"
    monkeypatch.setattr(builder, "OUT", out)
    assert builder.main() == 0

    external = set(re.findall(r"https?://[^\"'\s)]+", out.read_text(encoding="utf-8")))
    assert external <= {"http://www.w3.org/2000/svg"}, f"page reaches the network: {external}"
    assert "@font-face" in out.read_text(encoding="utf-8"), "fonts are not embedded"
