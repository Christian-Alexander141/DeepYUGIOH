from deepyugioh.engine.messages import CardRef, Loc
from deepyugioh.generator.search.actions import _lowest_bits, _subsets, _sum_at_least_minimal, _sum_exact
from deepyugioh.generator.search.combo import Benefit


def test_identical_copies_are_one_choice():
    cards = [CardRef(1, Loc(0, 0x01, i)) for i in range(3)] + [CardRef(2, Loc(0, 0x01, 3))]
    subsets, truncated = _subsets(cards, 1, 1, 100)
    assert len(subsets) == 2 and not truncated


def test_field_copies_stay_distinct():
    cards = [CardRef(1, Loc(0, 0x04, 0)), CardRef(1, Loc(0, 0x04, 1))]
    subsets, _ = _subsets(cards, 1, 1, 100)
    assert len(subsets) == 2


def test_sum_exact_matches_core_semantics():
    assert _sum_exact([3, 2], 5)
    assert not _sum_exact([3, 3], 5)
    assert _sum_exact([(2 << 16) | 1, 4], 6)  # a card that counts as 1 or 2


def test_sum_at_least_requires_minimal_selection():
    assert _sum_at_least_minimal([4, 4], 8)
    assert not _sum_at_least_minimal([4, 4, 4], 8)


def test_lowest_bits():
    assert _lowest_bits(0b10110, 2) == 0b00110


def test_benefit_dominance():
    base = dict(lp_gain=0, opp_lp_loss=0, hand_delta=-1, field_delta=1, opp_field_loss=0,
                opp_hand_loss=0, card_advantage=0, wins=False)
    a = Benefit(**base)
    b = Benefit(**{**base, "hand_delta": -2})
    assert a.dominates(b) and not b.dominates(a)



def test_zone_branching_rules():
    from deepyugioh.engine import constants as K
    from deepyugioh.engine.messages import Decision
    from deepyugioh.generator.search.actions import Enumerator

    # all 5 main zones and both Extra Monster Zones free (flag bit set = unavailable)
    d = Decision(K.MSG_SELECT_PLACE, 0, {"count": 1, "flag": ~0x7F & 0xFFFFFFFF})
    assert len(Enumerator(names=str).options(d)) == 1
    assert len(Enumerator(names=str, branch_zones=True).options(d)) == 3          # main vs EMZ1 vs EMZ2
    assert len(Enumerator(names=str, branch_zones=True, link_on_field=True).options(d)) == 7
