"""Stufe 2, step 2.3: restrictions (locks), card universe, card genericness, combo genericness, deck fit."""

import pytest

from deepyugioh import paths
from deepyugioh.analysis.specificity import (
    CardProfiler, CardUniverse, DeckProfile, GenericnessAnalyzer, card_fit, deck_fit, generic_scale,
)
from deepyugioh.cards.signatures import extract
from deepyugioh.contracts.effects import CardFilter, Condition, Lock

from test_analysis import _combo


def _locks(store, name):
    card = store.resolve(name)
    return [lk for s in extract((paths.CARD_SCRIPTS / card.script).read_text(), card) for lk in s.locks]


def test_locks_are_read_from_scripts(store):
    [mo_ye, *_] = _locks(store, "Swordsoul of Mo Ye")             # registered on its token, Extra Deck only
    assert (mo_ye.kind, mo_ye.scope) == ("SPECIAL_SUMMON", "extra")
    assert mo_ye.allowed.any_of[0].type_bits == 0x2000            # Synchro
    assert mo_ye.allows(store.resolve("Stardust Dragon")) and not mo_ye.allows(store.resolve("Borreload Dragon"))
    [patron, *_] = _locks(store, "Elfnote Power Patron")
    assert patron.scope == "extra" and patron.allowed is not None  # IsSynchroMonster is understood
    assert not _locks(store, "Foolish Burial")


def test_lock_from_synthetic_script(store):
    card = store.resolve("Foolish Burial")
    src = '''
    local s,id=GetID()
    function s.initial_effect(c)
        local e1=Effect.CreateEffect(c)
        e1:SetType(EFFECT_TYPE_ACTIVATE)
        e1:SetCode(EVENT_FREE_CHAIN)
        e1:SetCost(s.cost)
        e1:SetOperation(s.op)
        c:RegisterEffect(e1)
    end
    function s.cost(e,tp,eg,ep,ev,re,r,rp,chk)
        if chk==0 then return Duel.GetCustomActivityCount(id,tp,ACTIVITY_SPSUMMON)==0 end
    end
    function s.op(e,tp)
        local e1=Effect.CreateEffect(e:GetHandler())
        e1:SetType(EFFECT_TYPE_FIELD)
        e1:SetCode(EFFECT_CANNOT_SPECIAL_SUMMON)
        e1:SetTargetRange(1,0)
        e1:SetTarget(s.splimit)
        Duel.RegisterEffect(e1,tp)
    end
    function s.splimit(e,c) return not c:IsRace(RACE_DRAGON) end
    '''
    [sig] = extract(src, card)
    [lock] = sig.locks
    assert lock.kind == "SPECIAL_SUMMON" and lock.scope == "all" and lock.whole_turn
    assert lock.allows(store.resolve("Blue-Eyes White Dragon")) and not lock.allows(store.resolve("Kuriboh"))
    assert Lock("SPECIAL_SUMMON").forbids_all and Lock("ACTIVATE", unknown=True).allows(card) is None


def test_card_universe_shares(store):
    cards = [store.resolve(n) for n in ("Blue-Eyes White Dragon", "Kuriboh", "Pot of Greed", "Stardust Dragon")]
    u = CardUniverse(cards)
    assert u.share(u.monsters, u.all) == 0.75 and u.share(u.extra, u.monsters) == pytest.approx(1 / 3)
    dragons = Condition((CardFilter(races=0x2000),))   # RACE_DRAGON
    assert u.share(u.condition_mask(dragons), u.monsters) == pytest.approx(2 / 3)
    assert u.share(u.condition_mask(Condition()), u.all) == 1.0
    assert generic_scale(1.0) == 100 and generic_scale(0.1) == 75 and generic_scale(0.01) == 50 and generic_scale(1e-5) == 0


@pytest.fixture(scope="module")
def profiler():
    from deepyugioh.cards.graph import CardGraph
    from deepyugioh.cards.signatures import SignatureStore

    if CardGraph().version() == "unbuilt":
        pytest.skip("card database incomplete")
    return CardProfiler(SignatureStore())


def test_card_genericness_orders_generic_before_archetype_cards(profiler, code):
    g = {n: profiler.profile(code(n)).genericness for n in (
        "Monster Reborn", "Foolish Burial", "Reinforcement of the Army", "Sage with Eyes of Blue")}
    assert g["Monster Reborn"] == 100 > g["Foolish Burial"] > g["Reinforcement of the Army"] > g["Sage with Eyes of Blue"]
    lucina = profiler.profile(code("Elfnote Lucina"))            # one archetype search among generic effects
    assert 50 < lucina.genericness < 100 and "Elfnote" in lucina.explanation
    mo_ye = profiler.profile(code("Swordsoul of Mo Ye"))
    assert mo_ye.locks and mo_ye.lock_share < 1.0 and "Synchro" in mo_ye.explanation


def test_combo_genericness_locks_and_deck_fit(profiler, code):
    k = profiler.k
    analyzer = GenericnessAnalyzer(k, profiler)
    sage, bewd = code("Sage with Eyes of Blue"), code("Blue-Eyes White Dragon")
    blue = _combo(1, hand=(sage,), deck=(bewd,), line=[("MSG_SELECT_IDLECMD", "normal summon Sage with Eyes of Blue")])
    generic = _combo(1, hand=(code("Monster Reborn"), code("Pot of Greed")))
    rb, rg = analyzer.analyze(blue), analyzer.analyze(generic)
    assert rg.value == 100 > rb.value and "Blue Eyes" in rb.explanation
    dragon_deck = DeckProfile(races=0x2000, name="Dragon")
    warrior_deck = DeckProfile(races=0x1, name="Warrior")
    assert deck_fit(rb.raw, dragon_deck) > deck_fit(rb.raw, warrior_deck)   # BEWD is a dragon
    assert deck_fit(rg.raw, warrior_deck) == 100                           # generic spells fit everywhere
    sage_entry = next(c for c in rb.raw["cards"] if c["code"] == sage)
    assert card_fit(sage_entry, warrior_deck) < sage_entry["genericness"] / 100   # foreign archetype
    # a used card whose only usable effects restrict -> certain lock
    mo_ye = code("Swordsoul of Mo Ye")
    used = _combo(1, hand=(mo_ye,), line=[("MSG_SELECT_IDLECMD", "activate[0] Swordsoul of Mo Ye")])
    unused = _combo(1, hand=(mo_ye,))
    assert analyzer.combo_locks(used) and not analyzer.combo_locks(unused)
