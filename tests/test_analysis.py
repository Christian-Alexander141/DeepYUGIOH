"""Stufe 2: probabilities, axes, Database 2, incremental and versioned runs."""

from collections import Counter
from math import comb

import pytest

from deepyugioh.analysis import (
    AnalysisRepository, AnalyzerSystem, ConsistencyAnalyzer, CostAnalyzer, DeckModel, OutcomeAnalyzer, family_key,
)
from deepyugioh.analysis.probability import any_requirement, requirement
from deepyugioh.contracts import StartSetup
from deepyugioh.contracts.combos import ComboRecord, LineStep
from deepyugioh.repository.combo_db import ComboRepository


# ---- probabilities ---------------------------------------------------------------------
def test_single_card_probability_is_exact():
    p = requirement(Counter({1: 1}), deck_size=40, copies=3, hand_size=5)
    assert p == pytest.approx(1 - comb(37, 5) / comb(40, 5))
    two = requirement(Counter({1: 1, 2: 1}), hand_size=5)
    assert 0 < two < p
    assert requirement(Counter({1: 4}), copies=3) == 0.0          # more copies than allowed
    assert requirement(Counter(), hand_size=5) == 1.0


def test_family_union_is_at_least_every_member_and_ignores_supersets():
    a, b = Counter({1: 1}), Counter({2: 1})
    union = any_requirement([a, b])
    assert union > requirement(a) and union == pytest.approx(1 - comb(34, 5) / comb(40, 5))
    assert any_requirement([a, Counter({1: 1, 3: 1})]) == pytest.approx(requirement(a))
    assert any_requirement([a], hand_size=6) > any_requirement([a], hand_size=5)


# ---- axes on real cards --------------------------------------------------------------------
def _combo(code, *, hand=(), deck=(), extra=(), mzone=(), szone=(), own_hand=(), grave=(), benefit=None, line=(),
           setup_hand=None, sig="s1"):
    own = {"lp": 8000, "hand": list(own_hand), "mzone": [{"code": c, "seq": i, "pos": 1} for i, c in enumerate(mzone)],
           "szone": [{"code": c, "seq": i, "pos": p} for i, (c, p) in enumerate(szone)], "grave": list(grave),
           "banished": [], "deck_count": 10, "extra_count": 0}
    opp = {"lp": 8000, "hand": [], "mzone": [], "szone": [], "grave": [], "banished": [], "deck_count": 0, "extra_count": 0}
    b = {"lp_gain": 0, "opp_lp_loss": 0, "hand_delta": 0, "field_delta": len(mzone), "opp_field_loss": 0,
         "opp_hand_loss": 0, "card_advantage": 1, "wins": False, **(benefit or {})}
    setup = StartSetup(hand=tuple(setup_hand if setup_hand is not None else hand), deck=tuple(deck), extra=tuple(extra))
    return ComboRecord(sig, tuple(sorted(hand)), tuple(sorted(deck)), tuple(sorted(extra)), b,
                       tuple(k for k, v in b.items() if v and k != "card_advantage"), {"players": [own, opp], "winner": None},
                       False, tuple(LineStep(d, label, "00") for d, label in line), setup, True)


@pytest.fixture(scope="module")
def knowledge():
    from deepyugioh.cards.graph import CardGraph
    from deepyugioh.cards.signatures import SignatureStore

    if CardGraph().version() == "unbuilt":
        pytest.skip("card database incomplete")
    return SignatureStore()


def test_outcome_counts_disruptions_for_the_opponents_turn(store, code, knowledge):
    outcome = OutcomeAnalyzer(store, knowledge)
    ash, imperm = code("Ash Blossom & Joyous Spring"), code("Infinite Impermanence")
    board = _combo(1, mzone=[code("Blue-Eyes White Dragon")], own_hand=[ash], szone=[(imperm, 0xA)])
    res = outcome.analyze(board)
    assert res.raw["negates"] == 2 and res.raw["disruptions"] == 2       # hand trap in hand + set trap
    assert res.raw["atk_total"] == 3000
    plain = outcome.analyze(_combo(1, mzone=[code("Blue-Eyes White Dragon")]))
    assert plain.raw["disruptions"] == 0 and res.value > plain.value
    faceup_trap = outcome.analyze(_combo(1, szone=[(imperm, 1)]))           # face-up: already used
    assert faceup_trap.raw["disruptions"] == 0
    assert "Störung" in res.explanation


def test_cost_counts_cards_actions_and_normal_summon():
    line = [("MSG_SELECT_IDLECMD", "normal summon X"), ("MSG_SELECT_CHAIN", "no chain"),
            ("MSG_SELECT_IDLECMD", "activate[0] Y"), ("MSG_SELECT_EFFECTYN", "use Z: yes"),
            ("MSG_SELECT_IDLECMD", "end phase")]
    cheap = CostAnalyzer().analyze(_combo(1, hand=(1,), deck=(2,), line=line, benefit={"lp_gain": -1000}))
    assert cheap.raw == {"cards": 2, "hand": 1, "deck": 1, "extra": 0, "actions": 3, "lp_paid": 1000,
                         "normal_summon": True, "resources": 0, "hand_independence": -1}
    dear = CostAnalyzer().analyze(_combo(1, hand=(1, 2, 3), deck=(4,)))
    assert cheap.value > dear.value


def test_consistency_uses_the_family_and_falls_back_to_the_setup_hand():
    a = _combo(1, hand=(10,), mzone=[99], sig="a")
    b = _combo(1, hand=(20,), mzone=[99], sig="b")                 # same result, other starter
    c = _combo(1, hand=(30,), mzone=[98], sig="c")                 # other result
    revealed = _combo(1, hand=(), deck=(5,), setup_hand=(40,), mzone=[97], sig="d")
    assert family_key(a) == family_key(b) != family_key(c)
    cons = ConsistencyAnalyzer(DeckModel())
    cons.prepare(iter([a, b, c, revealed]))
    ra, rc = cons.analyze(a), cons.analyze(c)
    assert ra.raw["family_size"] == 2 and ra.value > rc.value == pytest.approx(rc.raw["own_first"])
    rd = cons.analyze(revealed)
    assert rd.raw["hand_from_setup"] and rd.value < 1.0


# ---- Database 2 and the Analyzer System ----------------------------------------------------------
def test_incremental_versioned_run_and_percentiles(store, code, tmp_path):
    from deepyugioh.contracts.combos import RunRecord

    repo = ComboRepository(tmp_path / "combos.sqlite")
    combos = [_combo(1, hand=(i,), deck=(100 + i,), mzone=[code("Blue-Eyes White Dragon")] * (i % 3), sig=f"sig{i}")
              for i in range(1, 7)]
    setup = combos[0].setup
    run = RunRecord(setup=setup, candidate=None, tier=None, limits={}, nodes=1, replays=0, elapsed_s=0.0, complete=True,
                    incomplete={}, pruned={}, combos_found=len(combos), generator_version="t", engine_commit="t",
                    scripts_commit="t", cdb_commit="t")
    repo.store_run(run, combos)
    analysis = AnalysisRepository(tmp_path / "analysis.sqlite")
    analyzers = [OutcomeAnalyzer(store, None), CostAnalyzer(), ConsistencyAnalyzer()]
    first = AnalyzerSystem(repo, analysis, analyzers).run()
    assert first.written == {"outcome": 6, "cost": 6, "consistency": 6} and not first.errors
    second = AnalyzerSystem(repo, analysis, analyzers).run()
    assert second.skipped == {"outcome": 6, "cost": 6} and second.written == {"consistency": 6}  # global axis
    bumped = CostAnalyzer()
    bumped.version = "99"
    third = AnalyzerSystem(repo, analysis, [bumped]).run()
    assert third.written == {"cost": 6}
    scores = [s.score for _, s in analysis.top("outcome", limit=10)]
    assert scores == sorted(scores, reverse=True) and all(0 < s <= 100 for s in scores)
    prof = analysis.profile("sig2")
    assert set(prof) == {"outcome", "cost", "consistency"} and prof["cost"].version == "99"
    assert analysis.stats()["axes"]["outcome"]["combos"] == 6


def test_deck_model_from_settings(tmp_path, monkeypatch):
    from deepyugioh import settings

    path = tmp_path / "s.toml"
    monkeypatch.setenv(settings.SETTINGS_ENV, str(path))
    assert settings.deck_model() == DeckModel()
    path.write_text("[analysis.deck]\ndeck_size = 60\nhand_first = 5\n")
    assert settings.deck_model().deck_size == 60
    path.write_text("[analysis.deck]\ncopies = 4\n")
    with pytest.raises(ValueError):
        settings.deck_model()
