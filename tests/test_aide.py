"""AIDE: Lua reader, effect signatures, constraint checks, index, planners, candidate pool, hybrid source."""

from collections import Counter
from dataclasses import replace

import pytest

from deepyugioh import paths
from deepyugioh.cards import lua
from deepyugioh.cards.signatures import extract
from deepyugioh.contracts import Candidate, StartSetup
from deepyugioh.contracts.effects import CardFilter, Condition, EffectSignature, Move, Trigger
from deepyugioh.generator import GeneratorConfig
from deepyugioh.generator.aide import constraints as C
from deepyugioh.generator.aide.hypotheses import HypothesisStore
from deepyugioh.generator.aide.planner import Plan, Step
from deepyugioh.generator.aide.source import HYBRID_TIER, HybridCandidateSource, plan_is_consistent
from deepyugioh.generator.config import CandidateConfig


# ---- Lua reader -------------------------------------------------------------------------
def test_lua_reader_functions_returns_and_expressions():
    src = '''
    --[[ long comment with function inside: function s.fake() end ]]
    local s,id=GetID()
    function s.filter(c)
        -- a comment with "quotes" and end
        return c:IsLevelBelow(4) and (c:IsRace(RACE_WARRIOR) or c:IsAttribute(ATTRIBUTE_DARK)) and not c:IsCode(id)
    end
    function s.op(e,tp)
        local g=Duel.SelectMatchingCard(tp,s.filter,tp,LOCATION_DECK,0,1,1,nil)
        if #g>0 then Duel.SendtoHand(g,nil,REASON_EFFECT) end
    end
    '''
    funcs = lua.functions(lua.tokenize(src))
    assert set(funcs) == {"s.filter", "s.op"}
    [ret] = lua.returns(funcs["s.filter"].body)
    node = lua.parse_expr(ret)
    assert node.op == "and" and node.args[1].op == "or" and node.args[2].op == "not"
    calls = [c.name for c in lua.calls(funcs["s.op"].body)]
    assert calls == ["Duel.SelectMatchingCard", "Duel.SendtoHand"]


# ---- effect signatures ------------------------------------------------------------------
def _sigs(store, name):
    card = store.resolve(name)
    return extract((paths.CARD_SCRIPTS / card.script).read_text(), card)


def test_signature_search_and_send(store):
    [fb] = _sigs(store, "Foolish Burial")
    assert fb.kind == "ACTIVATE" and "HAND" in fb.location
    [m] = fb.moves
    assert (m.src, m.dst) == (("DECK",), "GRAVE") and m.what.any_of[0].kinds == {"MONSTER"}
    [rota] = _sigs(store, "Reinforcement of the Army")
    f = rota.moves[0].what.any_of[0]
    assert rota.moves[0].dst == "HAND" and f.max_level == 4 and f.races == 0x1 and not rota.unknown


def test_signature_trigger_condition_count_limit_and_target(store):
    [sangan] = _sigs(store, "Sangan")
    assert sangan.trigger == Trigger("TO_GRAVE", True, True) and sangan.location == ("GRAVE",)
    assert sangan.count_limit == "HOPT:id" and "PREVIOUS:MZONE+SZONE" in sangan.conditions
    assert sangan.moves[0].what.any_of[0].max_atk == 1500
    [junk] = _sigs(store, "Junk Synchron")
    assert junk.trigger.event == "SUMMON" and junk.moves[0].src == ("GRAVE",) and junk.moves[0].targets


def test_signature_costs_and_summon_procedure(store):
    proc, ignition = _sigs(store, "Dark Grepher")
    assert proc.kind == "SUMMON_PROC" and proc.moves[0].is_self and proc.moves[0].dst == "MZONE"
    assert proc.costs and proc.costs[0].src == ("HAND",)
    assert ignition.costs[0].src == ("HAND",) and ignition.costs[0].what.any_of[0].attributes == 0x20  # DARK
    assert ignition.moves[0].src == ("DECK",) and ignition.count_limit == "OPT"


def test_signature_or_filters_and_unknown_parts(store):
    card = store.resolve("Foolish Burial")
    src = '''
    local s,id=GetID()
    function s.initial_effect(c)
        local e1=Effect.CreateEffect(c)
        e1:SetCategory(CATEGORY_TOHAND)
        e1:SetType(EFFECT_TYPE_ACTIVATE)
        e1:SetCode(EVENT_FREE_CHAIN)
        e1:SetCondition(s.weird)
        e1:SetOperation(s.op)
        c:RegisterEffect(e1)
    end
    function s.weird(e,tp) return Duel.GetTurnCount()>5 end
    function s.filter(c) return (c:IsLevel(1) or c:IsSetCard(0x123)) and c:SomethingNew() and c:IsAbleToHand() end
    function s.op(e,tp)
        local g=Duel.SelectMatchingCard(tp,s.filter,tp,LOCATION_GRAVE,0,1,1,nil)
        Duel.SendtoHand(g,nil,REASON_EFFECT)
    end
    '''
    [sig] = extract(src, card)
    [m] = sig.moves
    assert (m.src, m.dst) == (("GRAVE",), "HAND")
    alts = m.what.any_of
    assert len(alts) == 2 and {alts[0].min_level, alts[1].min_level} == {1, None}
    assert all("SomethingNew" in f.unknown for f in alts)          # never guessed, never excluding
    assert any(u.startswith("condition") for u in sig.unknown) and sig.confidence < 1.0
    assert m.what.matches(store.resolve("Kuriboh"))                  # level 1 alternative


# ---- constraints ------------------------------------------------------------------------------
def test_constraints_reject_only_what_is_known(store):
    gy = EffectSignature("1:0", 1, "IGNITION", ("GRAVE",), conditions=("NOT_TURN_SENT_TO_GY",))
    assert C.blocked(gy, store.resolve("Kuriboh"))
    unknown = replace(gy, conditions=("UNKNOWN:s.con",))
    assert C.blocked(unknown, store.resolve("Kuriboh")) is None    # uncertain -> allowed
    to_field = Move(Condition(), ("DECK",), "MZONE")
    assert not C.can_move(to_field, store.resolve("Pot of Greed"))  # spells are not summoned
    to_hand = Move(Condition(), ("EXTRA",), "HAND")
    assert not C.can_move(to_hand, store.resolve("Stardust Dragon"))
    warrior = Move(Condition((CardFilter(races=0x1, max_level=4),)), ("DECK",), "HAND")
    assert C.can_move(warrior, store.resolve("Marauding Captain"))
    assert not C.can_move(warrior, store.resolve("Blue-Eyes White Dragon"))


# ---- index and planners ------------------------------------------------------------------------
@pytest.fixture(scope="module")
def planner(store):
    from deepyugioh.cards.signatures import SignatureStore
    from deepyugioh.generator.aide.index import EffectIndex
    from deepyugioh.generator.aide.planner import Planner

    from deepyugioh.cards.graph import CardGraph

    if CardGraph().version() == "unbuilt":
        pytest.skip("interaction graph missing")
    return Planner(EffectIndex(SignatureStore()), max_cards=5)


def test_index_matches_producers_to_consumers(planner, code):
    idx = planner.index
    assert idx.is_consumer(code("Sangan"), "GRAVE")
    names = {p.code for p in idx.producers_for(code("Junk Synchron"), "HAND", 50)}
    assert code("Junk Converter") in names              # "add 1 Synchron monster from your deck"


def test_forward_plans_are_multi_step_and_consistent(planner, code):
    plans = planner.forward(code("Junk Converter"))
    assert plans and max(p.links for p in plans) >= 2
    for p in plans:
        assert 2 <= len(p.start) <= 5 and not plan_is_consistent(p, planner.index)
        assert any(s.links for s in p.steps)
        assert dict(p.start)[code("Junk Converter")] == "HAND"


def test_backward_plans_reach_the_goal(planner, code):
    goal = code("Junk Synchron")
    plans, _ = planner.backward(goal)
    assert plans
    for p in plans:
        assert goal in p.cards and any(goal == c for s in p.steps for _, c in s.links)


# ---- candidate pool --------------------------------------------------------------------------------
def _plan(cards):
    return Plan("forward", cards[0][0], tuple(sorted(cards)), (Step(cards[0][0], "x:0", (), ((cards[0][0], cards[1][0]),)),))


def test_hypothesis_status_model(tmp_path):
    store = HypothesisStore(tmp_path / "aide.sqlite")
    a, b, c, d = _plan([(1, "HAND"), (2, "DECK")]), _plan([(3, "HAND"), (4, "DECK")]), \
        _plan([(5, "HAND"), (6, "DECK")]), _plan([(7, "HAND"), (8, "DECK")])
    store.store_plans("v", "forward", 1, [a, b, c, d], [{"goal": 9, "reason": "test"}])
    assert [p.start for p in store.cached("v", "forward", 1)] == [a.start, b.start, c.start, d.start]
    for p, sid in ((a, "sa"), (b, "sb"), (c, "sc"), (d, "sd")):
        store.handed_over("v", p.id, [sid])
    assert store.counts("v")["SIMULATION_PENDING"] == 4
    jobs = {"sa": ("complete", 1), "sb": ("complete", 2), "sc": ("incomplete", 3), "sd": ("error", None)}
    combos = {1: [(1, 2, 2)], 2: [(3,)], 3: []}
    counts = store.update_status(lambda ids: {i: jobs[i] for i in ids if i in jobs},
                                 lambda rids: {r: combos.get(r, []) for r in rids})
    assert counts["VALIDATED"] == 1 and counts["INVALID"] == 1 and counts["INCONCLUSIVE"] == 1
    assert counts["UNSUPPORTED"] == 1 and counts["ABSTRACT_REJECTED"] == 1


# ---- hybrid source ------------------------------------------------------------------------------------
class _FakeSource:
    def __init__(self, prefix, n, ver="g1"):
        self.prefix, self.n, self.ver = prefix, n, ver

    def version(self):
        return self.ver

    def candidates(self, tier):
        for i in range(self.n):
            yield Candidate(StartSetup(hand=(self.prefix * 1000 + tier * 100 + i,)), tier, {"i": i})

    def tier_size(self, tier):
        return self.n


def test_hybrid_keeps_tiers_1_to_4_and_interleaves_tier_7():
    graph = _FakeSource(1, 3)
    aide = _FakeSource(2, 3, "a1")
    hybrid = HybridCandidateSource(graph, lambda: aide, share=0.5)
    assert hybrid.version() == hybrid.tier_version(1) == hybrid.tier_version(4) == "g1"  # cursors of 1-4 unchanged
    assert [c.id for c in hybrid.candidates(2)] == [c.id for c in graph.candidates(2)]
    assert hybrid.tier_version(5) == "a1" and hybrid.tier_version(7).startswith("hybrid:")
    mixed = list(hybrid.candidates(HYBRID_TIER))
    assert all(c.tier == HYBRID_TIER for c in mixed)
    vias = [c.origin["via"] for c in mixed]
    assert vias[:4] == ["graph", "aide", "graph", "aide"]
    assert Counter(vias) == {"graph": 12, "aide": 6}                 # 4 graph tiers x 3, AIDE tiers 5+6 x 3
    assert len({c.id for c in mixed}) == len(mixed)


def test_aide_is_opt_in_and_checked():
    assert GeneratorConfig().candidates.tiers == (1, 2, 3, 4)
    with pytest.raises(ValueError, match="aide_share"):
        GeneratorConfig().with_(candidates=CandidateConfig(aide_share=1.5)).check()
    assert GeneratorConfig().with_(candidates=CandidateConfig(tiers=(7,))).check()


# ---- engine fix found by AIDE ------------------------------------------------------------------------------
def test_identity_with_main_and_extra_monster_zone():
    from deepyugioh.engine.duel import BoardState, CardState, PlayerState

    def board(mz):
        p0 = PlayerState(8000, (), tuple(CardState(c, 4, seq, 1) for seq, c in mz), (), (), (), 30, 10)
        p1 = PlayerState(8000, (), (), (), (), (), 40, 0)
        return BoardState((p0, p1))

    start = board([])
    mixed = board([(5, 44508094), (2, 63977008)])     # Extra Monster Zone + main zone, no Link monster
    assert mixed.identity(start, zones_matter=False)  # used to raise TypeError ('<' between int and str)
    main_only = board([(3, 1), (1, 2)])
    assert main_only.identity(start, zones_matter=False) == board([(0, 2), (4, 1)]).identity(start, zones_matter=False)
