"""End-to-end tests of the search against the real engine (internal API)."""

from deepyugioh.contracts import StartSetup
from deepyugioh.generator import GeneratorConfig, SearchLimits
from deepyugioh.generator.search.dfs import ComboSearch
from deepyugioh.generator.search.validate import validate


def _names(store, combo):
    return sorted(store.name(c) for c in combo.requirements.cards)


def test_foolish_burial_monster_reborn(store, code):
    setup = StartSetup(hand=(code("Foolish Burial"), code("Monster Reborn")), deck=(code("Blue-Eyes White Dragon"),))
    result = ComboSearch(store, setup).run()
    assert result.complete
    assert len(result.combos) == 1
    combo = result.combos[0]
    assert _names(store, combo) == ["Blue-Eyes White Dragon", "Foolish Burial", "Monster Reborn"]
    assert combo.requirements.hand == setup.hand
    assert combo.requirements.deck == (code("Blue-Eyes White Dragon"),)
    assert combo.benefit.field_delta == 1


def test_monster_reborn_without_target_is_no_combo(store, code):
    result = ComboSearch(store, StartSetup(hand=(code("Monster Reborn"), code("Pot of Greed")))).run()
    assert result.combos == []


def test_junk_synchron_doppelwarrior_line(store, code):
    setup = StartSetup(
        hand=(code("Junk Synchron"), code("Foolish Burial")),
        deck=(code("Doppelwarrior"), code("Tuningware"), code("Blue-Eyes White Dragon")),
        extra=(code("Junk Warrior"), code("Formula Synchron"), code("Stardust Dragon")),
    )
    result = ComboSearch(store, setup).run()
    assert result.complete
    best = max(result.combos, key=lambda c: c.benefit.field_delta)
    assert _names(store, best) == ["Doppelwarrior", "Foolish Burial", "Junk Synchron", "Junk Warrior"]
    field = sorted(store.name(c.code) for c in best.end_board.players[0].mzone)
    assert field == ["Doppel Token", "Doppel Token", "Junk Warrior"]
    # "Foolish Burial on anything + Normal Summon Junk Synchron" is the summon alone -> redundant
    assert all(c.benefit.field_delta >= 2 for c in result.combos)
    for combo in result.combos:
        assert validate(store, setup, [s.response for s in combo.line], combo.board_key)


def test_card_limit_prunes(store, code):
    setup = StartSetup(
        hand=(code("Junk Synchron"), code("Foolish Burial")), deck=(code("Doppelwarrior"),), extra=(code("Junk Warrior"),)
    )
    result = ComboSearch(store, setup, SearchLimits(max_cards=3)).run()
    assert all(c.requirements.count <= 3 for c in result.combos)
    assert result.pruned["too_many_cards"] > 0


def test_opponent_board_enables_removal(store, code):
    boards = GeneratorConfig().boards(store)
    setup = StartSetup(hand=(code("Fissure"),), opponent=boards["standard"])
    result = ComboSearch(store, setup, SearchLimits(min_cards=1)).run()
    assert [c.benefit.opp_field_loss for c in result.combos] == [1]
    empty = ComboSearch(store, StartSetup(hand=(code("Fissure"),)), SearchLimits(min_cards=1)).run()
    assert empty.combos == []  # nothing to destroy on an empty board


def test_identity_is_independent_of_untouched_opponent_board(store, code):
    boards = GeneratorConfig().boards(store)
    kw = dict(hand=(code("Foolish Burial"), code("Monster Reborn")), deck=(code("Blue-Eyes White Dragon"),))
    a = ComboSearch(store, StartSetup(**kw)).run().combos
    b = ComboSearch(store, StartSetup(**kw, opponent=boards["standard"])).run().combos
    assert [c.signature for c in a] == [c.signature for c in b]
    assert a[0].board_key != b[0].board_key  # the full boards differ, the combo does not


def test_zone_branching_without_links_is_not_symmetric_duplicates(store, code):
    kw = dict(hand=(code("Foolish Burial"), code("Monster Reborn")), deck=(code("Blue-Eyes White Dragon"),))
    plain = ComboSearch(store, StartSetup(**kw)).run().combos
    zoned = ComboSearch(store, StartSetup(**kw, branch_zones=True)).run().combos
    assert len(plain) == len(zoned) == 1
