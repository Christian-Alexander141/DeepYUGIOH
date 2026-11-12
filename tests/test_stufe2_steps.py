"""Stufe 2, steps 2.5-2.9: line facts, compatibility, quality filter, robustness, later game situations."""

import json
import sqlite3

import pytest

from deepyugioh.analysis import AnalysisRepository, FilterProfile, QualityFilter, Rescorer, scorings_from_dict
from deepyugioh.analysis.compatibility import (
    CompatibilityIndex, CompatibilitySettings, ComboInterface, interface, run_compatibility,
)
from deepyugioh.analysis.robustness import (
    BASELINE, AdversarialSearch, RobustnessSettings, analyse_combo, build_axis, combo_setup, with_handtrap,
)
from deepyugioh.contracts import FieldCard, StartSetup
from deepyugioh.contracts.cards import CardInfo
from deepyugioh.contracts.effects import CardFilter, Condition
from deepyugioh.repository.combo_db import ComboRepository

from test_analysis import _combo

DRAGON, WARRIOR = 0x2000, 0x1


def _card(code, race=DRAGON, type_=0x21):
    return CardInfo(code, 0, f"card{code}", type_, 4, 0, 0, 0x10, race, 1000, 1000, 0, (), False, None)


CARDS = {1: _card(1), 2: _card(2, WARRIOR), 3: _card(3), 4: _card(4, WARRIOR), 9: _card(9, type_=0x2)}
ONLY_DRAGONS = {"kind": "SPECIAL_SUMMON", "scope": "all", "allowed": "Dragon", "forbidden": None, "share": 0.1,
                "whole_turn": True, "unknown": False, "card": 1, "certain": True,
                "allowed_filters": Condition((CardFilter(races=DRAGON),)).to_dict(), "forbidden_filters": None}


SMALL = CompatibilitySettings(min_shared=0.05, ubiquitous_share=1.0)   # four combos: no card counts as ubiquitous


def _index(settings=SMALL):
    items = [
        ComboInterface("a", (1, 9), {"Drache": 0.5}, [ONLY_DRAGONS], ((1, "hand"),), score=10),
        ComboInterface("b", (2, 9), {}, [], ((2, "deck"),), score=20),            # summons a Warrior
        ComboInterface("c", (1, 3), {"Drache": 1.0}, [], ((3, "grave"),), score=30),  # only Dragons
        ComboInterface("d", (4, 9), {}, [], (), (4,), score=5),                    # Normal Summons a Warrior
    ]
    return CompatibilityIndex(items, CARDS.get, settings)


# ---- step 2.5: compatibility --------------------------------------------------------------------
def test_shared_cards_weigh_rare_cards_more_than_ubiquitous_ones():
    idx = _index()
    assert idx.weight[9] < idx.weight[1] < idx.weight[3]                # card 9 is in three of four combos
    rare, common = idx.relation("a", "c"), idx.relation("a", "b")
    assert rare["shared_cards"] == [1] and common["shared_cards"] == [9]
    assert rare["shared_strength"] > common["shared_strength"] > 0
    assert rare["archetypes"] == ["Drache"] and rare["archetype_strength"] == 0.5


def test_lock_conflict_is_directed_and_only_hits_what_the_other_combo_does():
    idx = _index()
    ab = idx.relation("a", "b")
    assert [f["cards"] for f in ab["a_forbids_b"]] == [[2]] and ab["b_forbids_a"] == []
    assert ab["a_forbids_b"][0]["strength"] == 1.0                       # whole turn: no order helps
    assert idx.relation("a", "c")["a_forbids_b"] == []                   # c only summons Dragons
    assert idx.relation("a", "d")["a_forbids_b"] == []                   # a Normal Summon is not a Special Summon
    rest_of_turn = dict(ONLY_DRAGONS, whole_turn=False)
    idx.items[0].locks[0] = rest_of_turn
    assert idx.relation("a", "b")["a_forbids_b"][0]["strength"] == 0.5   # playable in the right order


def test_edges_and_summaries_cover_all_combos():
    rows, summaries = _index().build()
    kinds = {(a, b, k) for a, b, k, _, _ in rows}
    assert ("a", "c", "shared_cards") in kinds and ("a", "b", "lock_conflict") in kinds
    assert ("a", "c", "archetype") in kinds
    assert summaries["a"]["forbids"] == 1 and summaries["b"]["forbidden_by"] == 1
    assert summaries["b"]["compatible_share"] == pytest.approx(1 - 1 / 3)
    assert summaries["c"]["compatible_share"] == 1.0


def test_interface_without_line_facts_only_knows_extra_deck_summons():
    extra = _card(7, type_=0x2021)   # Synchro
    raw = {"cards": [{"code": 7}, {"code": 1}], "tags": {"archetypes": {"X": 1}}, "locks": [dict(ONLY_DRAGONS, certain=False)]}
    it = interface("s", raw, None, {7: extra, 1: CARDS[1]}.get)
    assert it.proxy and it.special == ((7, "extra"),) and it.locks == [] and it.archetypes == {"X": 0.5}
    full = interface("s", raw, {"special_summons": [[1, "grave"]], "normal_summons": [], "activations": [9]}, CARDS.get)
    assert not full.proxy and full.special == ((1, "grave"),) and full.activated == (9,)


def _analysis(tmp_path):
    """Database 2 with four combos: x strong, y a weaker member of x's family, z and w others."""
    repo = AnalysisRepository(tmp_path / "a.sqlite")
    aid = repo.start({}, {}, "test", 4)
    rows = []
    for sig, out, dis, cost, cons, fam, cards, locks in (
            ("x", 10.0, 2, -2.0, 0.6, "f1", [1, 3], []),
            ("y", 10.0, 2, -5.0, 0.3, "f1", [1, 3, 9], []),            # same result, more cards, worse everywhere
            ("z", 4.0, 0, -3.0, 0.5, "f2", [2, 9], [ONLY_DRAGONS]),
            ("w", 1.0, 0, -8.0, 0.1, "f3", [4], [])):
        generic = {"cards": [{"code": c, "genericness": 100.0 if c == 9 else 40.0, "setcodes": [],
                              "race_bits": CARDS[c].race if c != 9 else 0, "attribute_bits": 0} for c in cards],
                   "locks": locks, "tags": {"archetypes": {}}}
        rows += [(sig, "outcome", "1", {"disruptions": dis, "opp_losses": 0, "card_advantage": 1}, out, ""),
                 (sig, "cost", "1", {"resources": 0, "hand_independence": -len(cards)}, cost, ""),
                 (sig, "consistency", "1", {"family": fam, "own_first": cons, "family_first": cons, "family_second": cons}, cons, ""),
                 (sig, "genericness", "1", generic, 50.0, "")]
    repo.write(rows, aid)
    for axis in ("outcome", "cost", "consistency", "genericness"):
        repo.normalise(axis)
    repo.finish(aid, len(rows))
    Rescorer(repo, scorings_from_dict({})).run()
    return repo


def test_compatibility_run_is_stored_and_skipped_when_nothing_changed(tmp_path):
    repo = _analysis(tmp_path)
    first = run_compatibility(repo, CARDS.get, SMALL)
    assert not first["skipped"] and first["edges"]["shared_cards"] >= 1
    assert repo.profile("x")["compatibility"].raw["facts"] == "proxy"
    assert any(e["other"] == "y" and e["kind"] == "shared_cards" for e in repo.edges("x"))
    assert run_compatibility(repo, CARDS.get, SMALL)["skipped"]


def test_ubiquitous_cards_never_link_two_combos_on_their_own():
    helper_only = dict(_index(CompatibilitySettings(min_shared=0.05, ubiquitous_share=0.5)).shared_partners())
    assert helper_only[1] == [] and helper_only[3] == []          # b and d share nothing but card 9 (3 of 4 combos)
    assert [j for _, j in helper_only[0]] == [2]                  # a and c share the rare card 1
    assert [j for _, j in dict(_index().shared_partners())[1]] != []


# ---- step 2.6: quality filter ---------------------------------------------------------------------
CONSTS = {"RACE_DRAGON": DRAGON, "RACE_WARRIOR": WARRIOR}


def test_filter_minimum_dominance_and_ranking(tmp_path):
    qf = QualityFilter(_analysis(tmp_path), CONSTS)
    res = qf.apply(FilterProfile("t", minimum={"outcome.disruptions": 1}))
    assert [s for s, _, _ in res.members] == ["x"]                       # y is dominated inside the family
    assert res.dropped == {"minimum outcome.disruptions": 2, "dominated": 1}
    kept = qf.apply(FilterProfile("t", minimum={"outcome.disruptions": 1}, drop_dominated=False))
    assert [s for s, _, _ in kept.members] == ["x", "y"]
    own = qf.apply(FilterProfile("t", weights={"cost": 1.0}, drop_dominated=False))
    assert [s for s, _, _ in own.members] == ["x", "z", "y", "w"]        # cheapest first
    assert [s for s, _, _ in qf.apply(FilterProfile("t", max_cards=1)).members] == ["w"]


def test_filter_deck_profile_and_lock_policy(tmp_path):
    qf = QualityFilter(_analysis(tmp_path), CONSTS)
    dragons = qf.apply(FilterProfile("d", races=("Dragon",), min_fit=90.0))
    assert [s for s, _, _ in dragons.members] == ["x"] and dragons.members[0][2] == 100.0
    assert "z" not in {s for s, _, _ in qf.apply(FilterProfile("n", locks="none")).members}
    fitting = qf.apply(FilterProfile("f", races=("Dragon",), locks="fitting"))
    assert "z" in {s for s, _, _ in fitting.members}                     # "only Dragons" suits a Dragon deck
    warriors = qf.apply(FilterProfile("w", races=("Warrior",), locks="fitting"))
    assert "z" not in {s for s, _, _ in warriors.members}
    for bad in (dict(locks="fitting"), dict(min_fit=50.0), dict(locks="sometimes"), dict(weights={"cost": -1.0})):
        with pytest.raises(ValueError):
            FilterProfile("bad", **bad)


def test_stored_filter_results_go_stale_and_export(tmp_path):
    repo = _analysis(tmp_path)
    qf = QualityFilter(repo, CONSTS)
    qf.ensure_defaults()
    qf.save(FilterProfile("mine", "Test", {"strength": 50.0}))
    assert repo.filters()["mine"]["applied_at"] is None
    res = qf.run("mine")
    info = repo.filters()["mine"]
    assert info["members"] == len(res.members) > 0 and not info["stale"]
    assert [r[1] for r in repo.filter_results("mine")] == list(range(1, len(res.members) + 1))
    Rescorer(repo, scorings_from_dict({"strength": {"weights": {"cost": 1.0}}})).run()   # Database 2 changes
    assert repo.filters()["mine"]["stale"]
    qf.run("mine")
    out = tmp_path / "mine.json"
    assert qf.export("mine", out) == repo.filters()["mine"]["members"]
    data = json.loads(out.read_text())
    assert data["profile"]["name"] == "mine" and data["members"][0]["rank"] == 1 and "strength" in data["members"][0]["axes"]
    assert isinstance(qf.run_all()["eroeffnung"], object)
    repo.delete_filter("mine")
    assert "mine" not in repo.filters()


def test_scoring_marks_a_component_one_combo_lacks(tmp_path):
    repo = _analysis(tmp_path)
    aid = repo.start({}, {}, "test", 1)
    repo.write([("x", "robustness", "1", {}, 0.8, "")], aid)
    repo.normalise("robustness")
    summary = Rescorer(repo, scorings_from_dict({})).run()
    assert summary["strength"]["missing"] == [] and summary["strength"]["partial"] == 3
    assert repo.profile("x")["strength"].raw["missing"] == []
    y = repo.profile("y")["strength"]
    assert y.raw["missing"] == ["robustness"] and "ohne Robustheit" in y.explanation
    assert abs(sum(y.raw["weights"].values()) - 1) < 1e-3


# ---- engine: line facts, robustness, later game situations -----------------------------------------
@pytest.fixture(scope="module")
def outcome(store):
    from deepyugioh.analysis import OutcomeAnalyzer
    from deepyugioh.cards.graph import CardGraph
    from deepyugioh.cards.signatures import SignatureStore

    if CardGraph().version() == "unbuilt":
        pytest.skip("card database incomplete")
    return OutcomeAnalyzer(store, SignatureStore())


@pytest.fixture(scope="module")
def reborn(store, code):
    """Foolish Burial + Monster Reborn -> Blue-Eyes White Dragon on the field."""
    from deepyugioh.generator import ComboGenerator, GeneratorConfig

    setup = StartSetup(hand=(code("Foolish Burial"), code("Monster Reborn")), deck=(code("Blue-Eyes White Dragon"),))
    [combo] = ComboGenerator(store, None, GeneratorConfig()).search(setup).combos
    return combo


def test_line_facts_come_from_the_engine(store, code, reborn):
    from deepyugioh.analysis import line_facts

    facts = line_facts(store, reborn)
    assert facts["special_summons"] == [[code("Blue-Eyes White Dragon"), "grave"]]
    assert facts["normal_summons"] == [] and sorted(facts["activations"]) == sorted(reborn.hand)


def test_handtraps_decide_what_is_guaranteed(store, reborn, outcome):
    settings = RobustnessSettings()
    rows = {h: (status, value, data) for h, status, value, data, _ in
            analyse_combo(store, reborn, outcome.evaluate, settings, ["ash", "crow", "veiler"])}
    assert all(status == "complete" for status, _, _ in rows.values())
    undisturbed = rows[BASELINE][1]
    assert rows[BASELINE][2]["setup"] == "cards" and undisturbed >= rows[BASELINE][2]["combo_value"] > 0
    assert rows["ash"][1] < undisturbed and rows["ash"][2]["critical"]["after"].endswith("Foolish Burial")
    assert rows["crow"][1] < undisturbed and rows["crow"][2]["critical"]["opponent"] == "D.D. Crow"
    assert rows["veiler"][1] == undisturbed and not rows["veiler"][2]["used"]      # nothing for Effect Veiler to hit


def test_robustness_axis_only_from_complete_searches(store, reborn, outcome, tmp_path):
    repo = AnalysisRepository(tmp_path / "a.sqlite")
    settings = RobustnessSettings(catalog={k: v for k, v in RobustnessSettings().catalog.items() if k in ("ash", "veiler")})
    rows = analyse_combo(store, reborn, outcome.evaluate, settings)
    version = settings.search_version()
    repo.write_robustness([(reborn.signature, h, version, st, v, d, el) for h, st, v, d, el in rows])
    assert build_axis(repo, settings) == {"combos": 1, "partial": 0}
    axis = repo.profile(reborn.signature)["robustness"]
    assert axis.raw["handtraps"]["veiler"]["retention"] == 1.0 and axis.raw["handtraps"]["ash"]["retention"] == 0.0
    assert axis.raw["worst"] == "ash" and axis.raw["stops"] == ["ash"]
    assert axis.value == pytest.approx(0.6 / 1.6)                                   # weights 1.0 (Ash) and 0.6 (Veiler)
    # a search that hits its limit is incomplete and never becomes part of the axis
    tight = RobustnessSettings(max_nodes=3, catalog=settings.catalog)
    setup, acting = with_handtrap(combo_setup(reborn), store, tight, "ash")
    res = AdversarialSearch(store, setup, outcome.evaluate, tight, acting).run()
    assert res.value is None and res.incomplete == {"max_nodes": 1}
    repo.write_robustness([(reborn.signature, "ash", version, "incomplete", None, {"incomplete": res.incomplete}, 0.0)])
    assert build_axis(repo, settings) == {"combos": 0, "partial": 1}
    assert "robustness" not in repo.profile(reborn.signature)


def test_robustness_settings_from_the_settings_file(tmp_path, monkeypatch):
    from deepyugioh import settings

    path = tmp_path / "s.toml"
    monkeypatch.setenv(settings.SETTINGS_ENV, str(path))
    assert set(settings.robustness_settings().catalog) == set(RobustnessSettings().catalog)
    path.write_text('[limits]\nallow_mset = true\n[analysis.robustness]\nmax_nodes = 500\n'
                    '[analysis.robustness.handtraps.ash]\nweight = 0.5\n[analysis.robustness.handtraps.maxx_c]\nenabled = false\n')
    rs = settings.robustness_settings()
    assert rs.max_nodes == 500 and rs.allow_mset and rs.catalog["ash"]["weight"] == 0.5 and "maxx_c" not in rs.catalog
    assert rs.search_version() != RobustnessSettings().search_version()
    settings.set_handtrap_weights({"veiler": 0.9})
    assert settings.robustness_settings().catalog["veiler"]["weight"] == 0.9
    with pytest.raises(ValueError):
        settings.set_handtrap_weights({"veiler": 3.0})


def test_hand_cards_used_in_the_hand_or_hidden_by_a_draw_are_required(store, code):
    """The finding from Phase 1: both lines used a hand card that was not recorded as required."""
    from deepyugioh.generator.search.dfs import ComboSearch

    # 1. an effect activated in the hand; the card reveals itself and stays there
    habakiri = code("Ame no Habakiri no Mitsurugi")
    setup = StartSetup(hand=(habakiri,), deck=(code("Mitsurugi no Mikoto, Aramasa"), code("Mitsurugi no Mikoto, Kusanagi")))
    combos = ComboSearch(store, setup).run().combos
    assert combos and all(c.requirements.hand == (habakiri,) for c in combos)
    assert any(habakiri in [x.code for x in c.end_board.players[0].hand] for c in combos)   # still in the hand
    # 2. the hand card is summoned; a second copy is put on top of the deck and drawn
    witch, rose = code("Ruddy Rose Witch"), code("Witch of the Black Rose")
    combos = ComboSearch(store, StartSetup(hand=(witch,), deck=(rose, witch))).run().combos
    drawn = [c for c in combos if witch in [x.code for x in c.end_board.players[0].hand]]
    assert drawn and all(c.requirements.hand == (witch,) and witch in c.requirements.deck for c in drawn)


def test_recheck_corrects_stored_requirements(store, code, tmp_path, monkeypatch):
    from deepyugioh import paths
    from deepyugioh.cards.store import CardStore
    from deepyugioh.generator import ComboGenerator, GeneratorConfig
    from deepyugioh.generator.recheck import recheck

    monkeypatch.setattr(paths, "combo_db", lambda: tmp_path / "c.sqlite")
    repo = ComboRepository()
    habakiri = code("Ame no Habakiri no Mitsurugi")
    setup = StartSetup(hand=(habakiri,), deck=(code("Mitsurugi no Mikoto, Aramasa"), code("Mitsurugi no Mikoto, Kusanagi")))
    n = ComboGenerator(store, repo, GeneratorConfig()).search(setup).new_combos
    assert n >= 1 and recheck(repo, CardStore)["unchanged"] == n          # stored with the current rule: reproduced
    # make one record look like it was stored with the old rule (hand card missing, old identity)
    good = repo.get(1)
    repo.db.execute("UPDATE combos SET hand='[]', signature='old', card_count=card_count-1 WHERE id=1")
    repo.db.execute("DELETE FROM combo_cards WHERE combo_id=1 AND role='hand'")
    repo.db.commit()
    report = recheck(repo, CardStore)
    assert [c["id"] for c in report["changed"]] == [1] and report["updated"] == 0 and repo.get(1).hand == ()
    fixed = recheck(repo, CardStore, apply=True, repo=repo)
    assert fixed["updated"] == 1 and repo.get(1) == good and repo.get(1).signature == good.signature
    assert ("hand", habakiri) in repo.db.execute("SELECT role, code FROM combo_cards WHERE combo_id=1").fetchall()


def test_setups_without_resources_keep_their_id():
    plain = StartSetup(hand=(2, 1), deck=(3,))
    assert set(plain.to_dict()) == {"hand", "deck", "extra", "opponent", "seed", "starting_lp", "master_rule", "branch_zones"}
    late = StartSetup(hand=(1,), grave=(5, 4), field=(FieldCard(6, "szone", "facedown"),))
    assert late.late and not plain.late and late.id != StartSetup(hand=(1,)).id
    assert StartSetup.from_dict(late.to_dict()) == late and late.grave == (4, 5)


def test_later_game_combo_needs_the_card_in_the_gy(store, code, tmp_path):
    from deepyugioh.generator import ComboGenerator, GeneratorConfig, SearchLimits
    from deepyugioh.generator.execution import combo_signature
    from deepyugioh.generator.search.dfs import ComboSearch

    reborn_, dragon, elf = code("Monster Reborn"), code("Blue-Eyes White Dragon"), code("Mystical Elf")
    limits = SearchLimits(min_cards=1)
    found = ComboSearch(store, StartSetup(hand=(reborn_,), grave=(dragon, elf)), limits).run().combos
    by_grave = {c.requirements.grave: c for c in found}
    assert set(by_grave) == {(dragon,), (elf,)} and all(c.requirements.hand == (reborn_,) for c in found)
    # a start card the line never touches is not part of the combo: same signature without it
    alone = ComboSearch(store, StartSetup(hand=(reborn_,), grave=(dragon,)), limits).run().combos
    assert combo_signature(alone[0]) == combo_signature(by_grave[(dragon,)])
    # stored and read back with its origin
    repo = ComboRepository(tmp_path / "c.sqlite")
    cfg = GeneratorConfig().with_(limits=limits)
    rep = ComboGenerator(store, repo, cfg).search(StartSetup(hand=(reborn_,), grave=(dragon,)))
    stored = repo.get(1)
    assert rep.new_combos == 1 and stored.grave == (dragon,) and stored.card_count == 2 and stored.setup.grave == (dragon,)
    assert [r for r in repo.db.execute("SELECT role, code FROM combo_cards ORDER BY role")] == [("grave", dragon), ("hand", reborn_)]


def test_database_1_schema_2_is_migrated_without_losing_combos(tmp_path):
    path = tmp_path / "old.sqlite"
    ComboRepository(path).close()
    db = sqlite3.connect(path)   # turn the new file into what schema 2 looked like
    db.executescript("""
        DROP TABLE combo_cards;
        CREATE TABLE combo_cards (combo_id INTEGER NOT NULL, code INTEGER NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('hand', 'deck', 'extra')), copies INTEGER NOT NULL,
            PRIMARY KEY (combo_id, code, role));
        ALTER TABLE combos DROP COLUMN resources;
        INSERT INTO combo_cards VALUES (1, 11, 'hand', 1);
        UPDATE meta SET value = '2' WHERE key = 'schema_version';
    """)
    db.commit()
    db.close()
    repo = ComboRepository(path)
    assert repo.db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ("3",)
    assert repo.db.execute("SELECT * FROM combo_cards").fetchall() == [(1, 11, "hand", 1)]
    ddl = repo.db.execute("SELECT sql FROM sqlite_master WHERE name='combo_cards'").fetchone()[0]
    assert "'grave'" in ddl and "'field'" in ddl                                # the new roles are accepted
    assert "resources" in {r[1] for r in repo.db.execute("PRAGMA table_info(combos)")}


def test_late_game_candidates_put_the_resource_where_its_effect_works(store):
    from deepyugioh.cards.graph import CardGraph
    from deepyugioh.cards.signatures import SignatureStore
    from deepyugioh.generator import GeneratorConfig, GraphCandidateSource
    from deepyugioh.generator.lategame import LATE_TIER, LateGameCandidateSource

    graph = CardGraph()
    if graph.version() == "unbuilt":
        pytest.skip("card database incomplete")
    cfg = GeneratorConfig()
    base = GraphCandidateSource(graph, store, cfg.candidates, cfg.boards(store))
    source = LateGameCandidateSource(SignatureStore(), graph, store, base)
    first = [c for _, c in zip(range(40), source.candidates(LATE_TIER))]
    assert first and all(c.tier == LATE_TIER and c.setup.late and len(c.setup.hand) <= 3 for c in first)
    for c in first:
        code_, zone = c.origin["resource"], c.origin["zone"]
        assert code_ in getattr(c.setup, zone) and code_ not in c.setup.hand
    again = [c.id for _, c in zip(range(40), source.candidates(LATE_TIER))]
    assert again == [c.id for c in first]                                           # fixed order: resumable


def test_cost_and_consistency_know_later_game_combos():
    from dataclasses import replace

    from deepyugioh.analysis import ConsistencyAnalyzer, CostAnalyzer

    opening = _combo(1, hand=(10,), mzone=[99], sig="a")
    late = replace(_combo(1, hand=(), mzone=[99], sig="b"), grave=(20,))
    assert late.cards == (20,) and late.card_count == 1
    cost = CostAnalyzer().analyze(late)
    assert cost.raw["resources"] == 1 and cost.raw["hand_independence"] == 0 > CostAnalyzer().analyze(opening).raw["hand_independence"]
    cons = ConsistencyAnalyzer()
    cons.prepare(iter([opening, late]))
    r = cons.analyze(late)
    assert r.raw["needs_resources"] and r.raw["own_first"] == 0.0 and r.raw["family_size"] == 1   # the opening member
    assert r.value == pytest.approx(cons.analyze(opening).raw["own_first"])
